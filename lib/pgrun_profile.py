#!/usr/bin/env python3
"""PGRun CI data profile: what a migration did to a production-like database.

    pgrun_profile.py capture <out.json>
    pgrun_profile.py compare <before.json> <after.json> --applied <file> \
        [--allowances <file>] [--result <out.json>] [--markdown <out.md>]
    pgrun_profile.py migrations [--root <dir>]
    pgrun_profile.py plan --files <migrations> --before <versions> --allowances <file>

capture reads the database in DATABASE_URL (never from argv, never printed)
through one psql session and records, per table: the row count and every
column's non-null count, the declared constraints and foreign keys, invalid
indexes, NOT VALID constraints the rows violate, the migration history, and
(with --rails) the relations and polymorphic references Rails conventions
imply. compare fails on what the migrations destroyed unless an approved
allowance names it.

Bounded by design, so a 100 GB copy does not mean unrestricted full scans:

  * a table whose planner estimate is at most --max-exact-rows is counted
    exactly; a bigger one is counted on a fixed TABLESAMPLE (--sample-rows),
    which detects loss at scale but cannot prove no single row was lost;
  * every statement runs under --statement-timeout, and once --budget seconds
    have passed the remaining tables are recorded as not profiled;
  * what was exact, sampled or skipped is in the output and the summary.

Only names, types and counts are ever written or printed: never a row value,
never a server error message (they can quote values), only its SQLSTATE.
Standard library only (Python 3.8+); needs psql on PATH.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")
# Migration bookkeeping tables of common frameworks: (schema-qualified regclass, column).
MIGRATION_TABLES = [
    ("rails", "schema_migrations", "version"),
    ("django", "django_migrations", "app || '.' || name"),
    ("flyway", "flyway_schema_history", "version"),
    ("prisma", "_prisma_migrations", "migration_name"),
    ("knex", "knex_migrations", "name"),
    ("alembic", "alembic_version", "version_num"),
    ("goose", "goose_db_version", "version_id::text"),
]


# ----------------------------------------------------------------- psql session
class Psql:
    """One psql process; each query's output ends at a marker carrying SQLSTATE."""

    def __init__(self, url, statement_timeout):
        env = dict(os.environ)
        env.update(conninfo_env(url))
        env.pop("DATABASE_URL", None)
        self.proc = subprocess.Popen(
            ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=0", "-P", "pager=off"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1)
        # Errors quote values; drain stderr so psql never blocks, and keep none of it.
        threading.Thread(target=lambda: [None for _ in self.proc.stderr], daemon=True).start()
        self.query("SET statement_timeout = '%ds'" % int(statement_timeout))
        self.query("SET lock_timeout = '10s'")
        self.query("SET application_name = 'pgrun-ci-profile'")

    def query(self, sql):
        """-> (rows, sqlstate). rows are lines; sqlstate '00000' on success."""
        marker = "__pgrun_%s__" % uuid.uuid4().hex
        self.proc.stdin.write(sql.strip().rstrip(";") + ";\n")
        self.proc.stdin.write("\\echo %s :SQLSTATE\n" % marker)
        self.proc.stdin.flush()
        rows = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("psql exited (cannot connect to the branch?)")
            line = line.rstrip("\n")
            if line.startswith(marker):
                return rows, line[len(marker):].strip() or "00000"
            if line != "":
                rows.append(line)

    def json(self, sql):
        rows, state = self.query(sql)
        if state != "00000":
            return None, state
        # json_agg separates elements with newlines; strings never contain raw ones.
        return (json.loads("\n".join(rows)) if rows else None), state

    def close(self):
        try:
            self.proc.stdin.write("\\q\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        self.proc.wait(timeout=10)


def conninfo_env(url):
    """DATABASE_URL -> libpq PG* variables, so the URL is never on a command line."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("postgres", "postgresql"):
        sys.exit("pgrun-profile: DATABASE_URL must be a postgres:// URL")
    env = {}
    if u.hostname:
        env["PGHOST"] = u.hostname
    if u.port:
        env["PGPORT"] = str(u.port)
    if u.username:
        env["PGUSER"] = urllib.parse.unquote(u.username)
    if u.password:
        env["PGPASSWORD"] = urllib.parse.unquote(u.password)
    db = urllib.parse.unquote(u.path.lstrip("/"))
    if db:
        env["PGDATABASE"] = db
    params = dict(urllib.parse.parse_qsl(u.query))
    for key, var in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"), ("connect_timeout", "PGCONNECT_TIMEOUT")):
        if key in params:
            env[var] = params[key]
    return env


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def ident(s):
    return '"' + s.replace('"', '""') + '"'


def qualified(schema, table):
    return ident(schema) + "." + ident(table)


# -------------------------------------------------------------- rails naming
IRREGULAR = {"person": "people", "man": "men", "woman": "women", "child": "children", "mouse": "mice",
             "goose": "geese", "ox": "oxen", "datum": "data", "medium": "media", "criterion": "criteria",
             "status": "statuses", "address": "addresses", "analysis": "analyses", "index": "indices"}


def pluralize(word):
    if word in IRREGULAR:
        return IRREGULAR[word]
    for singular, plural in IRREGULAR.items():
        if word.endswith("_" + singular):
            return word[: -len(singular)] + plural
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def tableize(class_name):
    """ActiveSupport's tableize for the common case: Admin::BlogPost -> admin_blog_posts."""
    snake = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    snake = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", snake).replace("::", "_").lower()
    return pluralize(snake)


CONSTANT_RE = r"^[A-Z][A-Za-z0-9]*(::[A-Z][A-Za-z0-9]*)*$"
INTEGERISH = {"smallint", "integer", "bigint"}


def comparable_keys(a, b):
    return a == b or (a in INTEGERISH and b in INTEGERISH)


# -------------------------------------------------------------------- capture
def capture(args):
    started = time.monotonic()
    deadline = started + args.budget
    db = Psql(os.environ.get("DATABASE_URL") or sys.exit("pgrun-profile: DATABASE_URL is not set"), args.statement_timeout)
    out = {"format": 1, "settings": {"max_exact_rows": args.max_exact_rows, "sample_rows": args.sample_rows,
                                     "statement_timeout_s": args.statement_timeout, "budget_s": args.budget}}
    out["server"], _ = db.json("SELECT json_build_object('version', current_setting('server_version'), "
                               "'version_num', current_setting('server_version_num')::int, "
                               "'database_bytes', pg_database_size(current_database()))")

    tables, _ = db.json("""
      SELECT coalesce(json_agg(t ORDER BY t.schema, t.name), '[]') FROM (
        SELECT n.nspname AS schema, c.relname AS name, c.reltuples::bigint AS estimate,
               c.relkind = 'p' AS partitioned,
               (SELECT json_agg(json_build_object('name', a.attname, 'type', format_type(a.atttypid, a.atttypmod),
                                                  'base_type', format_type(a.atttypid, NULL), 'not_null', a.attnotnull)
                                ORDER BY a.attnum)
                  FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS columns
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_%'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
      ) t""")
    out["tables"] = {}
    for t in tables or []:
        key = t["schema"] + "." + t["name"]
        entry = {"columns": {c["name"]: {"type": c["type"], "not_null": c["not_null"]} for c in (t["columns"] or [])},
                 "estimate": t["estimate"]}
        out["tables"][key] = entry
        if t["partitioned"]:
            entry["mode"] = "partitioned"  # its partitions are profiled as tables of their own
            continue
        if time.monotonic() > deadline:
            entry.update(mode="skipped", reason="budget")
            continue
        names = [c["name"] for c in (t["columns"] or [])]
        exprs = ["count(*)"] + ["count(%s)" % ident(n) for n in names]
        source = qualified(t["schema"], t["name"])
        estimate = t["estimate"]
        if estimate is not None and estimate > args.max_exact_rows:
            pct = max(0.0001, min(100.0, 100.0 * args.sample_rows / estimate))
            source += " TABLESAMPLE SYSTEM (%.6f) REPEATABLE (17)" % pct
            entry["mode"], entry["sample_percent"] = "sampled", round(pct, 6)
        else:
            entry["mode"] = "exact"
        values, state = db.json("SELECT json_build_array(%s) FROM %s" % (", ".join(exprs), source))
        if values is None:
            entry.update(mode="skipped", reason=reason_for(state))
            continue
        entry["rows"] = values[0]
        entry["non_null"] = dict(zip(names, values[1:]))

    out["constraints"], _ = db.json("""
      SELECT coalesce(json_agg(json_build_object(
               'table', n.nspname || '.' || cl.relname, 'name', co.conname, 'type', co.contype,
               'validated', co.convalidated,
               'columns', (SELECT json_agg(a.attname ORDER BY k.ord) FROM unnest(co.conkey) WITH ORDINALITY k(attnum, ord)
                             JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = k.attnum),
               'references', CASE WHEN co.contype = 'f' THEN (SELECT rn.nspname || '.' || rc.relname FROM pg_class rc
                               JOIN pg_namespace rn ON rn.oid = rc.relnamespace WHERE rc.oid = co.confrelid) END)
             ORDER BY 2, 3), '[]')
      FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
      WHERE co.contype IN ('p', 'u', 'f', 'c', 'x')
        AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_%'""")

    out["invalid_indexes"], _ = db.json("""
      SELECT coalesce(json_agg(n.nspname || '.' || c.relname ORDER BY 1), '[]')
      FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE NOT (i.indisvalid AND i.indisready)""")

    # A NOT VALID constraint is only checked on new rows; validate each one in
    # a transaction that is rolled back, as production would on VALIDATE.
    out["not_valid"] = {}
    for co in out["constraints"] or []:
        if co["validated"] or co["type"] not in ("c", "f"):
            continue
        key = co["table"] + "." + co["name"]
        if time.monotonic() > deadline:
            out["not_valid"][key] = "skipped:budget"
            continue
        schema, table = co["table"].split(".", 1)
        db.query("BEGIN")
        _, state = db.query("ALTER TABLE %s VALIDATE CONSTRAINT %s" % (qualified(schema, table), ident(co["name"])))
        db.query("ROLLBACK")
        out["not_valid"][key] = "holds" if state == "00000" else ("violated" if state in ("23514", "23503") else "skipped:" + reason_for(state))

    out["migrations"] = {}
    for framework, table, column in MIGRATION_TABLES:
        found, _ = db.json("SELECT to_json(to_regclass(%s)::text)" % lit(table))
        if not found:
            continue
        versions, state = db.json("SELECT coalesce(json_agg(v ORDER BY v), '[]') FROM (SELECT (%s)::text AS v FROM %s) s" % (column, table))
        if versions is not None:
            out["migrations"][framework] = versions

    if args.rails:
        out["rails"] = rails_relations(db, out, deadline)

    db.close()
    out["seconds"] = round(time.monotonic() - started, 1)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    modes = {}
    for t in out["tables"].values():
        modes[t["mode"]] = modes.get(t["mode"], 0) + 1
    print("pgrun-profile: %d tables (%s), %d rows counted, %d constraints, %d NOT VALID, %.1fs" % (
        len(out["tables"]), ", ".join("%s %d" % kv for kv in sorted(modes.items())),
        sum(t.get("rows", 0) for t in out["tables"].values() if t["mode"] == "exact"),
        len(out["constraints"] or []), len(out["not_valid"]), out["seconds"]))


def reason_for(state):
    return {"57014": "statement_timeout", "55P03": "lock_timeout", "42501": "permission"}.get(state, "sqlstate_" + str(state))


def rails_relations(db, out, deadline):
    """Relations Rails conventions imply where no foreign key declares them."""
    tables = out["tables"]
    declared = set()
    for co in out["constraints"] or []:
        if co["type"] == "f" and co["columns"] and len(co["columns"]) == 1:
            declared.add(co["table"] + "." + co["columns"][0])
    relations, polymorphic = {}, {}
    for key, t in sorted(tables.items()):
        schema = key.split(".", 1)[0]
        cols = t["columns"]
        for col, meta in cols.items():
            if not col.endswith("_id") or key + "." + col in declared:
                continue
            stem = col[:-3]
            if stem + "_type" in cols:
                # Rails polymorphic keys are integers or UUIDs; a string *_id
                # beside a *_type (payment_account_id + payment_account_type) is
                # an external reference, not an association.
                if meta["type"] in INTEGERISH or meta["type"] == "uuid":
                    polymorphic[key + "." + stem] = None
                continue
            parent = schema + "." + pluralize(stem)
            p = tables.get(parent)
            if not p or "id" not in p["columns"] or not comparable_keys(meta["type"], p["columns"]["id"]["type"]):
                continue
            relations[key + "." + col + " -> " + parent + ".id"] = None
    result = {"relations": {}, "polymorphic": {}}
    for rel in relations:
        if time.monotonic() > deadline:
            result["relations"][rel] = {"status": "skipped", "reason": "budget"}
            continue
        child, parent = rel.split(" -> ")
        cschema, ctable, ccol = child.split(".")
        pschema, ptable, _ = parent.split(".")
        source = qualified(cschema, ctable)
        sampled = tables[cschema + "." + ctable].get("mode") == "sampled"
        if sampled:
            source += " TABLESAMPLE SYSTEM (%s) REPEATABLE (17)" % tables[cschema + "." + ctable]["sample_percent"]
        n, state = db.json("SELECT count(*) FROM %s c WHERE c.%s IS NOT NULL AND NOT EXISTS (SELECT 1 FROM %s p WHERE p.id = c.%s)" % (
            source, ident(ccol), qualified(pschema, ptable), ident(ccol)))
        result["relations"][rel] = {"orphans": n, "sampled": sampled} if n is not None else {"status": "skipped", "reason": reason_for(state)}
    for poly in polymorphic:
        if time.monotonic() > deadline:
            result["polymorphic"][poly] = {"status": "skipped", "reason": "budget"}
            continue
        schema, table, stem = poly.split(".")
        src = qualified(schema, table)
        types, state = db.json("SELECT coalesce(json_object_agg(coalesce(t, ''), n), '{}') FROM (SELECT %s AS t, count(*) AS n FROM %s WHERE %s IS NOT NULL GROUP BY 1 LIMIT 1000) s" % (
            ident(stem + "_type"), src, ident(stem + "_type")))
        if types is None:
            result["polymorphic"][poly] = {"status": "skipped", "reason": reason_for(state)}
            continue
        rows = sum(types.values())
        bad = sum(n for t, n in types.items() if not re.match(CONSTANT_RE, t))
        resolved, unresolvable = 0, 0
        for type_name, n in types.items():
            if not re.match(CONSTANT_RE, type_name):
                continue
            target = schema + "." + tableize(type_name)
            p = tables.get(target)
            if not p or "id" not in p["columns"] or not comparable_keys(tables[schema + "." + table]["columns"][stem + "_id"]["type"], p["columns"]["id"]["type"]):
                unresolvable += n  # STI base class, a custom table name, or a key type we cannot compare
                continue
            hit, _ = db.json("SELECT count(*) FROM %s c WHERE c.%s = %s AND EXISTS (SELECT 1 FROM %s p WHERE p.id = c.%s)" % (
                src, ident(stem + "_type"), lit(type_name), qualified(*target.split(".", 1)), ident(stem + "_id")))
            resolved += hit or 0
        result["polymorphic"][poly] = {"rows": rows, "distinct_types": len(types), "non_class_rows": bad,
                                       "resolved_rows": resolved, "unresolvable_rows": unresolvable}
    return result


# -------------------------------------------------------------------- compare
def compare(args):
    before = load(args.before)
    after = load(args.after)
    # --applied: one migration per line, "version<TAB>sha256 of its file" ("-" if unknown).
    hashes = {}
    for line in (open(args.applied) if args.applied else []):
        parts = line.strip().split("\t")
        if parts and parts[0]:
            hashes[parts[0]] = parts[1] if len(parts) > 1 and parts[1] != "-" else None
    applied = list(hashes)
    allowances = load(args.allowances) if args.allowances and os.path.exists(args.allowances) else []
    problems, allowed_losses, warnings, notes = [], [], [], []
    allowed = {}  # (kind, target) -> index into granted
    granted = []  # [(kind, target, "migration V: reason")]; used ones are removed from `unused_ix`
    # An approval covers one migration file exactly as it was reviewed: same
    # version AND same SHA-256. Loss is excused only when that migration ran on
    # its own in this step, so another migration cannot borrow its approval.
    for a in allowances or []:
        version = str(a.get("migration_version") or a.get("migration") or "")
        if version not in hashes:
            continue
        approved = str(a.get("migration_sha256") or "").strip().lower()
        what = "approval for migration %s (%s %s)" % (version, a.get("kind"), a.get("target"))
        if not approved:
            warnings.append("%s has no migration SHA-256 and is not applied — re-approve it in PGRun" % what)
        elif hashes[version] is None:
            warnings.append("%s not applied: the migration's file was not found in the checkout, so its content cannot be matched" % what)
        elif hashes[version] != approved:
            warnings.append("%s not applied: the file changed after it was approved (approved sha256 %s…, this file %s…) — re-approve the new content" % (what, approved[:12], hashes[version][:12]))
        elif len(applied) > 1:
            warnings.append("%s not applied: %d migrations ran in this step, so the loss cannot be attributed to %s alone" % (what, len(applied), version))
        else:
            granted.append((a.get("kind"), a.get("target"), "migration %s: %s" % (version, a.get("reason", "").strip())))
            for target in normalize_target(str(a.get("target", ""))):
                allowed[(a.get("kind"), target)] = len(granted) - 1
    unused_ix = set(range(len(granted)))

    def destructive(kind, target, message):
        ix = allowed.get((kind, target))
        if ix is not None:
            unused_ix.discard(ix)
            allowed_losses.append(message + " — allowed by " + granted[ix][2])
        else:
            problems.append({"kind": kind, "target": target, "message": message})

    tol = args.sample_tolerance
    for key, b in sorted(before["tables"].items()):
        a = after["tables"].get(key)
        if a is None:
            if b.get("rows", b.get("estimate", 0)) and b.get("rows", b.get("estimate", 0)) > 0:
                destructive("drop", key, "table dropped: %s (%s rows)" % (key, b.get("rows", "~%s" % b.get("estimate"))))
            else:
                notes.append("table dropped: %s (empty)" % key)
            continue
        if b["mode"] not in ("exact", "sampled") or a["mode"] not in ("exact", "sampled"):
            if b["mode"] == "skipped" or a["mode"] == "skipped":
                warnings.append("not profiled: %s (%s) — loss in this table is not checked" % (key, a.get("reason") or b.get("reason")))
            continue
        for col, bc in b["columns"].items():
            if col not in a["columns"]:
                had = b["non_null"].get(col, 0)
                if had > 0:
                    destructive("drop", key + "." + col, "column dropped: %s.%s (%s%d non-null values)" % (key, col, "~" if b["mode"] == "sampled" else "", had))
                else:
                    notes.append("column dropped: %s.%s (no values)" % (key, col))
            elif a["columns"][col]["type"] != bc["type"]:
                notes.append("type changed: %s.%s %s -> %s" % (key, col, bc["type"], a["columns"][col]["type"]))
        for col in a["columns"]:
            if col not in b["columns"]:
                notes.append("column added: %s.%s %s%s" % (key, col, a["columns"][col]["type"], " NOT NULL" if a["columns"][col]["not_null"] else ""))
        exact = b["mode"] == "exact" and a["mode"] == "exact"
        if exact:
            if a["rows"] < b["rows"]:
                destructive("row_loss", key, "rows lost: %s %d -> %d" % (key, b["rows"], a["rows"]))
            for col, bv in b["non_null"].items():
                av = a["non_null"].get(col)
                if av is None or av >= bv:
                    continue
                if a["rows"] < b["rows"] and bv - av <= b["rows"] - a["rows"]:
                    continue  # explained by the rows lost
                destructive("value_loss", key + "." + col, "values lost: %s.%s non-null %d -> %d" % (key, col, bv, av))
        else:
            # Sampled on at least one side: compare the estimate and non-null
            # FRACTIONS, with a tolerance. Detects loss at scale only.
            br, ar = b.get("estimate") or 0, a.get("estimate") or 0
            if br > 0 and ar < br * (1 - tol):
                destructive("row_loss", key, "rows lost (estimated, sampled table): %s ~%d -> ~%d" % (key, br, ar))
            for col, bv in b["non_null"].items():
                av = a["non_null"].get(col)
                if av is None or not b["rows"] or not a["rows"]:
                    continue
                bf, af = bv / b["rows"], av / a["rows"]
                if bf - af > tol:
                    destructive("value_loss", key + "." + col, "values lost (sampled): %s.%s non-null %.1f%% -> %.1f%%" % (key, col, 100 * bf, 100 * af))
    for key, a in sorted(after["tables"].items()):
        if key not in before["tables"]:
            notes.append("table added: %s" % key)

    bcons = {(c["table"], c["name"]): c for c in before.get("constraints") or []}
    acons = {(c["table"], c["name"]): c for c in after.get("constraints") or []}
    for k, c in sorted(bcons.items()):
        if k not in acons and k[0] in after["tables"]:
            label = {"f": "foreign key", "u": "unique constraint", "p": "primary key", "c": "check constraint", "x": "exclusion constraint"}[c["type"]]
            warnings.append("%s removed: %s on %s(%s)" % (label, c["name"], c["table"], ", ".join(c["columns"] or [])))
    for k, c in sorted(acons.items()):
        if k not in bcons:
            notes.append("constraint added: %s on %s%s" % (c["name"], c["table"], "" if c["validated"] else " (NOT VALID)"))

    for key, status in sorted((after.get("not_valid") or {}).items()):
        was = (before.get("not_valid") or {}).get(key)
        if status == "violated":
            msg = "NOT VALID constraint the existing rows violate: %s — VALIDATE CONSTRAINT will fail in production" % key
            (warnings if was == "violated" else problems).append(msg if was == "violated" else {"kind": "constraint", "target": key, "message": msg})
        elif status.startswith("skipped"):
            warnings.append("NOT VALID constraint not checked: %s (%s)" % (key, status.split(":", 1)[1]))
    new_invalid = sorted(set(after.get("invalid_indexes") or []) - set(before.get("invalid_indexes") or []))
    for idx in new_invalid:
        problems.append({"kind": "index", "target": idx, "message": "invalid index after the migrations: %s (a failed CREATE INDEX CONCURRENTLY?)" % idx})
    for idx in sorted(set(before.get("invalid_indexes") or []) & set(after.get("invalid_indexes") or [])):
        warnings.append("invalid index already in the copy: %s" % idx)

    rb, ra = before.get("rails") or {}, after.get("rails") or {}
    for rel, r in sorted((ra.get("relations") or {}).items()):
        was = (rb.get("relations") or {}).get(rel) or {}
        if "orphans" in r and r["orphans"] > (was.get("orphans") or 0):
            problems.append({"kind": "reference", "target": rel, "message": "orphans added: %s %d -> %d%s" % (rel, was.get("orphans") or 0, r["orphans"], " (sampled)" if r.get("sampled") else "")})
    for poly, p in sorted((ra.get("polymorphic") or {}).items()):
        if "rows" not in p:
            continue
        was = (rb.get("polymorphic") or {}).get(poly) or {}
        name = poly.rsplit(".", 1)
        if p["non_class_rows"] > (was.get("non_class_rows") or 0):
            problems.append({"kind": "reference", "target": poly, "message": "polymorphic %s_type: %d of %d rows no longer hold a class name" % (poly, p["non_class_rows"], p["rows"])})
        elif p["non_class_rows"] == p["rows"] and p["rows"] > 0:
            # Not this PR's doing — the copy itself. Every such association is
            # dead on it, so the run cannot vouch for code that uses it.
            problems.append({"kind": "copy", "target": poly, "message": "copy integrity: %s_type holds no class names in any of %d rows — is %s_type masked on the project's Protect page? Rails polymorphic association %s cannot load" % (poly, p["rows"], name[0] + "." + name[1], name[1])})
        if p["resolved_rows"] == 0 and (was.get("resolved_rows") or 0) > 0:
            problems.append({"kind": "reference", "target": poly, "message": "polymorphic %s: no row resolves any more (%d before)" % (poly, was["resolved_rows"])})

    unused = ["%s %s (%s)" % granted[i] for i in sorted(unused_ix)]
    losses = [p for p in problems if p["kind"] in ("drop", "row_loss", "value_loss")]
    if losses and len(applied) == 1:
        v = applied[0]
        hint = ("if this loss is intended, a PGRun admin can approve it for migration %s, sha256 %s, as: %s" % (
            v, hashes[v] or "(file not found)", "; ".join("%s %s" % (p["kind"], p["target"]) for p in losses)))
    elif losses and len(applied) > 1:
        hint = ("if this loss is intended, a PGRun admin can approve it for the migration that causes it, by version and "
                "SHA-256 (the %d migrations applied are listed with theirs). An approved migration then runs on its own "
                "step with Rails' default migrate command; otherwise it must be the only migration the run applies" % len(applied))
    else:
        hint = None
    result = {"problems": problems, "allowed_losses": allowed_losses, "warnings": warnings, "notes": notes,
              "unused_allowances": unused, "applied": applied, "migration_sha256": hashes, "hint": hint,
              "tables": len(after["tables"]),
              "rows": sum(t.get("rows", 0) for t in after["tables"].values() if t.get("mode") == "exact"),
              "modes": count_modes(after), "seconds": [before.get("seconds"), after.get("seconds")],
              "relations_checked": len((ra.get("relations") or {})), "polymorphic_checked": len((ra.get("polymorphic") or {}))}
    if args.result:
        with open(args.result, "w") as f:
            json.dump(result, f, indent=1)
    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(markdown(result))
    for n in notes:
        print("  note: " + n)
    for w in warnings:
        print("::warning title=PGRun data check::" + w)
    for l in allowed_losses:
        print("::warning title=PGRun approved data loss::" + l)
    for u in unused:
        print("  note: approved allowance not used by this run: " + u)
    for p in problems:
        print("::error title=PGRun data check::" + p["message"])
    if problems and hint:
        print("::notice title=PGRun data check::" + hint)
    print("pgrun-profile: %d migration(s) applied; %d tables (%s); %d problem(s), %d approved loss(es), %d warning(s)" % (
        len(applied), result["tables"], ", ".join("%s %d" % kv for kv in sorted(result["modes"].items())),
        len(problems), len(allowed_losses), len(warnings)))
    return 1 if problems else 0


# ------------------------------------------------------------- migration files
MIGRATION_DIR = re.compile(r"(^|/)(db/([a-z0-9_]+_)?migrate|migrations|db/migrations|migrate)/")


def migration_files(args):
    """Print "version<TAB>sha256<TAB>path" for each migration file in the checkout.

    The version is what the framework records once the file has run (Rails
    20261015000001, Django app.0001_initial, Flyway 1.2, Prisma the directory
    name). A version claimed by two different files is printed with "-": its
    content cannot be told apart, so no approval can match it.
    """
    root = args.root
    try:
        paths = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
    except (OSError, subprocess.CalledProcessError):
        paths = [os.path.relpath(os.path.join(d, f), root) for d, ds, fs in os.walk(root)
                 for f in fs if not any(x in d.split(os.sep) for x in (".git", "node_modules", "vendor", "tmp"))]
    found = {}
    for path in filter(None, paths):
        if not MIGRATION_DIR.search(path):
            continue
        parts = path.split("/")
        base = parts[-1]
        keys = []
        m = re.match(r"^(\d+)_\w*\.(rb|sql|py|js|ts|go)$", base)
        if m:
            keys.append(m.group(1))
            if parts[-2] == "migrations" and base.endswith(".py") and len(parts) >= 3:
                keys.append("%s.%s" % (parts[-3], base[:-3]))   # Django app.name
        m = re.match(r"^V([\d_]+)__.*\.sql$", base)
        if m:
            keys.append(m.group(1).replace("_", "."))            # Flyway
        if base == "migration.sql" and len(parts) >= 3 and parts[-3] == "migrations":
            keys.append(parts[-2])                               # Prisma
        if not keys:
            continue
        with open(os.path.join(root, path), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        for k in keys:
            found.setdefault(k, set()).add((digest, path))
    for k in sorted(found):
        entries = found[k]
        if len(entries) == 1:
            digest, path = next(iter(entries))
            print("%s\t%s\t%s" % (k, digest, path))
        else:
            print("%s\t-\t%d files" % (k, len(entries)))


def plan(args):
    """Split the pending migrations into steps so approved ones run alone.

    Prints one step per line: "approved|batch<TAB>version version ...". With
    no approval for a pending migration (the usual case) it prints a single
    batch, and the migrations run in one go as they always have.
    """
    files = {}
    for line in open(args.files):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2:
            files[parts[0]] = parts[1]
    applied = {l.strip() for l in open(args.before) if l.strip()}
    pending = sorted((v for v in files if v not in applied), key=lambda v: (len(v), v) if v.isdigit() else (0, v))
    approved = set()
    for a in load(args.allowances) if os.path.exists(args.allowances) else []:
        v = str(a.get("migration_version") or a.get("migration") or "")
        sha = str(a.get("migration_sha256") or "").strip().lower()
        if v in pending and sha and files.get(v) == sha:
            approved.add(v)
    if not approved or len(pending) <= 1:
        print("batch\t" + " ".join(pending))
        return
    batch = []
    for v in pending:
        if v in approved:
            if batch:
                print("batch\t" + " ".join(batch))
                batch = []
            print("approved\t" + v)
        else:
            batch.append(v)
    if batch:
        print("batch\t" + " ".join(batch))


def normalize_target(target):
    """An allowance may name public tables without their schema."""
    # "orders" and "orders.total" may mean public.orders / public.orders.total;
    # "sales.orders" is ambiguous with a public table.column, so both stand.
    target = target.strip()
    return {target} if target.count(".") >= 2 else {target, "public." + target}


def count_modes(profile):
    modes = {}
    for t in profile["tables"].values():
        modes[t["mode"]] = modes.get(t["mode"], 0) + 1
    return modes


def markdown(r):
    lines = []
    if r["problems"]:
        lines.append("**Data checks failed** — %d problem(s):\n" % len(r["problems"]))
        lines += ["- ❌ %s" % p["message"] for p in r["problems"][:50]]
        if r.get("hint"):
            lines.append("\n" + r["hint"][0].upper() + r["hint"][1:] + "\n")
    for l in r["allowed_losses"]:
        lines.append("- ⚠️ approved: %s" % l)
    for w in r["warnings"][:30]:
        lines.append("- ⚠️ %s" % w)
    if r["notes"]:
        lines.append("\n<details><summary>%d schema change(s) and notes</summary>\n" % len(r["notes"]))
        lines += ["- %s" % n for n in r["notes"][:200]]
        lines.append("\n</details>")
    return "\n".join(lines) + "\n"


def load(path):
    with open(path) as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser(prog="pgrun_profile.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("out")
    c.add_argument("--max-exact-rows", type=int, default=int(os.environ.get("PGRUN_PROFILE_MAX_EXACT_ROWS", 5_000_000)))
    c.add_argument("--sample-rows", type=int, default=int(os.environ.get("PGRUN_PROFILE_SAMPLE_ROWS", 200_000)))
    c.add_argument("--statement-timeout", type=int, default=int(os.environ.get("PGRUN_PROFILE_STATEMENT_TIMEOUT", 120)))
    c.add_argument("--budget", type=int, default=int(os.environ.get("PGRUN_PROFILE_BUDGET", 900)))
    c.add_argument("--rails", action="store_true")
    k = sub.add_parser("compare")
    k.add_argument("before")
    k.add_argument("after")
    k.add_argument("--applied")
    k.add_argument("--allowances")
    k.add_argument("--result")
    k.add_argument("--markdown")
    k.add_argument("--sample-tolerance", type=float, default=0.02)
    m = sub.add_parser("migrations")
    m.add_argument("--root", default=".")
    pl = sub.add_parser("plan")
    pl.add_argument("--files", required=True)
    pl.add_argument("--before", required=True)
    pl.add_argument("--allowances", required=True)
    args = p.parse_args()
    if args.cmd == "capture":
        capture(args)
    elif args.cmd == "migrations":
        migration_files(args)
    elif args.cmd == "plan":
        plan(args)
    else:
        sys.exit(compare(args))


if __name__ == "__main__":
    main()
