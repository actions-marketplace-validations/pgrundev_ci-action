#!/usr/bin/env bash
# Profiler tests against a local Postgres: TEST_PG_URL=postgres://user@host:port/postgres
# Creates and drops its own database. Each case: build a schema with rows,
# capture, apply a "migration", capture, compare, check the verdict.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
prof="$here/../lib/pgrun_profile.py"
admin="${TEST_PG_URL:?TEST_PG_URL is required}"
db="pgrun_profile_test_$$"
base="${admin%/*}"
url="$base/$db"
tmp=$(mktemp -d)
trap 'psql "$admin" -qc "DROP DATABASE IF EXISTS $db" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
pass=0; fail=0
SHA=$(printf 'add priority' | shasum -a 256 | cut -d' ' -f1)   # the "migration file" approvals name
OTHER_SHA=$(printf 'edited after approval' | shasum -a 256 | cut -d' ' -f1)

reset() {
  psql "$admin" -qc "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db" >/dev/null
  psql "$url" -q -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE TABLE schema_migrations (version varchar PRIMARY KEY);
INSERT INTO schema_migrations VALUES ('20260101000000');
CREATE TABLE shops (id bigserial PRIMARY KEY, name text NOT NULL);
CREATE TABLE products (id bigserial PRIMARY KEY, shop_id bigint, title text, price numeric);
CREATE TABLE collections (id bigserial PRIMARY KEY, title text);
CREATE TABLE comments (id bigserial PRIMARY KEY, commentable_type varchar NOT NULL, commentable_id bigint NOT NULL, body text);
INSERT INTO shops (name) SELECT 's' || g FROM generate_series(1, 5) g;
INSERT INTO products (shop_id, title, price) SELECT 1 + g % 5, 't' || g, g FROM generate_series(1, 200) g;
INSERT INTO collections (title) SELECT 'c' || g FROM generate_series(1, 10) g;
INSERT INTO comments (commentable_type, commentable_id, body) SELECT CASE WHEN g % 2 = 0 THEN 'Product' ELSE 'Collection' END, 1 + g % 10, 'body-' || g FROM generate_series(1, 40) g;
ANALYZE;
SQL
}

# case <name> <expected exit> <expected regex in output> <migration sql> [allowances json]
case_() {
  local name="$1" want="$2" pattern="$3" sql="$4" allow="${5:-[]}"
  reset
  DATABASE_URL="$url" python3 "$prof" capture "$tmp/b.json" --rails > /dev/null
  psql "$url" -q -v ON_ERROR_STOP=1 -c "$sql" -c "INSERT INTO schema_migrations VALUES ('20261015000001')" -c "ANALYZE" > /dev/null
  DATABASE_URL="$url" python3 "$prof" capture "$tmp/a.json" --rails ${EXTRA_CAPTURE:-} > /dev/null
  printf '%s\n' "${APPLIED:-20261015000001	$SHA}" > "$tmp/applied"
  echo "$allow" > "$tmp/allow.json"
  local out code=0
  out=$(python3 "$prof" compare "$tmp/b.json" "$tmp/a.json" --applied "$tmp/applied" --allowances "$tmp/allow.json" --result "$tmp/r.json" --markdown "$tmp/r.md" 2>&1) || code=$?
  if [ "$code" = "$want" ] && grep -qE "$pattern" <<< "$out" && { [ -z "${NOT_PATTERN:-}" ] || ! grep -qE "$NOT_PATTERN" <<< "$out"; }; then
    pass=$((pass + 1)); echo "ok   $name"
  else
    fail=$((fail + 1)); echo "FAIL $name (exit $code, want $want; pattern /$pattern/)"; echo "$out" | sed 's/^/     /'
  fi
  # Nothing the database holds may reach the output.
  if grep -qE "body-[0-9]|'t[0-9]+'" <<< "$out$(cat "$tmp/r.md")"; then
    fail=$((fail + 1)); echo "FAIL $name: a row value reached the output"
  fi
}

case_ "additive migration passes"            0 "0 problem"                       "ALTER TABLE products ADD COLUMN sku text; UPDATE products SET sku = 'x'"
case_ "backfill keeps values"                0 "0 problem"                       "ALTER TABLE products ADD COLUMN cents bigint; UPDATE products SET cents = price * 100"
case_ "clearing a column fails"              1 "values lost: public.products.title non-null 200 -> 0" "UPDATE products SET title = NULL"
case_ "deleting rows fails"                  1 "rows lost: public.products 200 -> 150" "DELETE FROM products WHERE id <= 50"
case_ "dropping a column with data fails"    1 "column dropped: public.products.price" "ALTER TABLE products DROP COLUMN price"
case_ "dropping a table with rows fails"     1 "table dropped: public.collections"  "DROP TABLE collections"
case_ "lossy type change fails"              1 "values lost: public.products.title" "ALTER TABLE products ALTER COLUMN title TYPE int USING NULL"
NOT_PATTERN="not used by this run" \
case_ "approved loss passes with a warning"  0 "approved data loss.*products.title.*migration 20261015000001: drop legacy titles" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20261015000001","migration_sha256":"'"$SHA"'","kind":"value_loss","target":"products.title","reason":"drop legacy titles"}]'
case_ "approval for an edited migration does not apply" 1 "values lost.*|file changed after it was approved" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20261015000001","migration_sha256":"'"$OTHER_SHA"'","kind":"value_loss","target":"products.title","reason":"x"}]'
case_ "edited migration: the run says why"     1 "file changed after it was approved \(approved sha256 ${OTHER_SHA:0:12}…, this file ${SHA:0:12}…\)" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20261015000001","migration_sha256":"'"$OTHER_SHA"'","kind":"value_loss","target":"products.title","reason":"x"}]'
case_ "approval without a SHA-256 does not apply" 1 "has no migration SHA-256 and is not applied" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20261015000001","kind":"value_loss","target":"products.title","reason":"x"}]'
APPLIED="20261015000001	$SHA
20261015000002	$OTHER_SHA" \
case_ "approval is not borrowed by another migration in the same step" 1 "not applied: 2 migrations ran in this step" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20261015000001","migration_sha256":"'"$SHA"'","kind":"value_loss","target":"products.title","reason":"x"}]'
APPLIED="20261015000001	-" \
case_ "approval for a migration whose file is missing does not apply" 1 "file was not found in the checkout" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20261015000001","migration_sha256":"'"$SHA"'","kind":"value_loss","target":"products.title","reason":"x"}]'
case_ "an unapproved loss tells the admin what to approve" 1 "approve it for migration 20261015000001, sha256 $SHA, as: value_loss public.products.title" \
      "UPDATE products SET title = NULL"
case_ "allowance for another migration does not apply" 1 "values lost" \
      "UPDATE products SET title = NULL" '[{"migration_version":"20990101000000","migration_sha256":"'"$SHA"'","kind":"value_loss","target":"products.title","reason":"other migration"}]'
case_ "allowance covers only what it names"  1 "values lost: public.products.price" \
      "UPDATE products SET title = NULL, price = NULL" '[{"migration_version":"20261015000001","migration_sha256":"'"$SHA"'","kind":"value_loss","target":"products.title","reason":"drop legacy titles"}]'
case_ "orphans added fail"                   1 "orphans added: public.products.shop_id -> public.shops.id 0 -> " "DELETE FROM shops WHERE id = 1; ALTER TABLE shops ALTER COLUMN name DROP NOT NULL"
case_ "NOT VALID constraint the rows violate fails" 1 "NOT VALID constraint the existing rows violate: public.products.price_positive" \
      "ALTER TABLE products ADD CONSTRAINT price_positive CHECK (price > 100) NOT VALID"
case_ "NOT VALID constraint that holds passes" 0 "0 problem" "ALTER TABLE products ADD CONSTRAINT price_positive CHECK (price > 0) NOT VALID"
case_ "polymorphic type masked fails"        1 "comments.commentable_type: 40 of 40 rows no longer hold a class name" \
      "UPDATE comments SET commentable_type = 'pgrun_fake_' || substr(md5(commentable_type), 1, 16)"
case_ "polymorphic ids masked fail"          1 "polymorphic public.comments.commentable: no row resolves" \
      "UPDATE comments SET commentable_id = commentable_id + 900000"
EXTRA_CAPTURE="--max-exact-rows 50 --sample-rows 1000" \
case_ "sampled table: clearing a column fails" 1 "values lost \\(sampled\\): public.products.title" "UPDATE products SET title = NULL"

# A copy whose polymorphic types are ALL masked is reported even with no migration.
reset
psql "$url" -qc "UPDATE comments SET commentable_type = 'pgrun_fake_' || substr(md5(commentable_type), 1, 16)" > /dev/null
DATABASE_URL="$url" python3 "$prof" capture "$tmp/b.json" --rails > /dev/null
code=0; out=$(python3 "$prof" compare "$tmp/b.json" "$tmp/b.json" --applied /dev/null 2>&1) || code=$?
if [ $code = 1 ] && grep -q "copy integrity: public.comments.commentable_type holds no class names" <<< "$out"; then pass=$((pass+1)); echo "ok   masked copy is reported"; else fail=$((fail+1)); echo "FAIL masked copy is reported"; echo "$out"; fi

# A string *_id beside a *_type is an external reference, not a polymorphic association.
reset
psql "$url" -qc "CREATE TABLE accounts (id bigserial PRIMARY KEY, payment_account_id varchar, payment_account_type varchar); INSERT INTO accounts (payment_account_id, payment_account_type) SELECT 'acct_' || g, 'express' FROM generate_series(1, 5) g" > /dev/null
DATABASE_URL="$url" python3 "$prof" capture "$tmp/b.json" --rails > /dev/null
code=0; out=$(python3 "$prof" compare "$tmp/b.json" "$tmp/b.json" --applied /dev/null 2>&1) || code=$?
if [ $code = 0 ] && ! grep -q payment_account <<< "$out"; then pass=$((pass+1)); echo "ok   string *_id + *_type is not treated as polymorphic"; else fail=$((fail+1)); echo "FAIL string *_id + *_type"; echo "$out"; fi

# Budget exhausted: tables are recorded as skipped and the compare warns.
reset
DATABASE_URL="$url" python3 "$prof" capture "$tmp/b.json" --budget 0 > /dev/null
code=0; out=$(python3 "$prof" compare "$tmp/b.json" "$tmp/b.json" --applied /dev/null 2>&1) || code=$?
if grep -q "not profiled: public.products (budget)" <<< "$out"; then pass=$((pass+1)); echo "ok   budget exhaustion is visible"; else fail=$((fail+1)); echo "FAIL budget exhaustion is visible"; echo "$out"; fi

# The URL (and its password) never reaches argv or output.
reset
out=$(DATABASE_URL="$url" python3 "$prof" capture "$tmp/b.json" 2>&1)
if grep -q "$db" <<< "$out"; then fail=$((fail+1)); echo "FAIL the database URL reached the output"; else pass=$((pass+1)); echo "ok   no URL in output"; fi

# Migration files: version -> SHA-256 of the file; a version claimed twice cannot be matched.
repo="$tmp/repo"; mkdir -p "$repo/db/migrate" "$repo/db/cache_migrate" "$repo/blog/migrations" "$repo/prisma/migrations/20261015_init" "$repo/sql"
printf 'add priority' > "$repo/db/migrate/20261015000001_add_priority.rb"
printf 'a' > "$repo/db/migrate/20261015000002_dup.rb"; printf 'b' > "$repo/db/cache_migrate/20261015000002_dup.rb"
printf 'x' > "$repo/blog/migrations/0001_initial.py"
printf 'y' > "$repo/prisma/migrations/20261015_init/migration.sql"
printf 'z' > "$repo/sql/20261015000009_not_a_migration.sql"
git -C "$repo" init -q && git -C "$repo" add -A
out=$(python3 "$prof" migrations --root "$repo")
if grep -q "^20261015000001	$SHA	db/migrate/20261015000001_add_priority.rb$" <<< "$out"; then
  if grep -q "^20261015000002	-	2 files$" <<< "$out" && grep -q "^blog.0001_initial	" <<< "$out" && grep -q "^20261015_init	" <<< "$out" && ! grep -q 20261015000009 <<< "$out"; then
    pass=$((pass+1)); echo "ok   migration files: Rails, multi-db duplicate, Django, Prisma; other SQL ignored"
  else fail=$((fail+1)); echo "FAIL migration files"; echo "$out"; fi
else fail=$((fail+1)); echo "FAIL migration files (hash)"; echo "$out"; fi

# Plan: an approved pending migration runs on its own step; otherwise one step.
printf '20261015000001\t%s\tx\n20261015000003\tccc\tx\n20261015000004\tddd\tx\n20260101000000\teee\tx\n' "$SHA" > "$tmp/files"
echo 20260101000000 > "$tmp/before"
echo '[]' > "$tmp/allow.json"
out=$(python3 "$prof" plan --files "$tmp/files" --before "$tmp/before" --allowances "$tmp/allow.json")
[ "$out" = "batch	20261015000001 20261015000003 20261015000004" ] && { pass=$((pass+1)); echo "ok   plan: no approval → one step"; } || { fail=$((fail+1)); echo "FAIL plan one step: $out"; }
echo '[{"migration_version":"20261015000003","migration_sha256":"ccc"}]' > "$tmp/allow.json"
out=$(python3 "$prof" plan --files "$tmp/files" --before "$tmp/before" --allowances "$tmp/allow.json")
[ "$out" = "batch	20261015000001
approved	20261015000003
batch	20261015000004" ] && { pass=$((pass+1)); echo "ok   plan: the approved migration runs alone"; } || { fail=$((fail+1)); echo "FAIL plan split: $out"; }
echo '[{"migration_version":"20261015000003","migration_sha256":"not-this-file"}]' > "$tmp/allow.json"
out=$(python3 "$prof" plan --files "$tmp/files" --before "$tmp/before" --allowances "$tmp/allow.json")
[ "$(grep -c . <<< "$out")" = 1 ] && { pass=$((pass+1)); echo "ok   plan: an approval for other content does not split"; } || { fail=$((fail+1)); echo "FAIL plan other content: $out"; }

echo "profile tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
