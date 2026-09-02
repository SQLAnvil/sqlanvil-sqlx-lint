"""sqlanvil-specific behaviour: warehouse-aware E006, columnTypes on
declarations, the dataset: alias, and the S-series rules."""

from sqlanvil_sqlx_lint import Config, lint_text

PATH = "definitions/outputs/customer_metrics.sqlx"
PG = Config(warehouse="postgres")
SB = Config(warehouse="supabase")
MY = Config(warehouse="mysql")
BQ = Config(warehouse="bigquery")


def codes(findings):
    return sorted(f.code for f in findings)


def only(findings, code):
    return [f for f in findings if f.code == code]


def table(body, config_extra="", ctype="table"):
    return (
        'config {\n    type: "%s",\n    columns: { id: "Id." }%s\n}\n\n%s\n'
        % (ctype, config_extra, body)
    )


class TestE006Postgres:
    def test_bare_two_part_after_from(self):
        text = table("select id from public.orders o")
        found = only(lint_text(text, PATH, config=PG), "E006")
        assert len(found) == 1 and "public.orders" in found[0].message

    def test_double_quoted_two_part_after_join(self):
        text = table(
            'select id from ${ref("orders")} o\n'
            'join "analytics"."customers" c on c.id = o.customer_id'
        )
        found = only(lint_text(text, PATH, config=SB), "E006")
        assert len(found) == 1 and '"analytics"."customers"' in found[0].message

    def test_insert_into_and_update_in_operations(self):
        text = (
            'config { type: "operations" }\n\n'
            "insert into staging.events select 1\n---\n"
            "update reporting.daily set x = 1\n"
        )
        found = only(lint_text(text, "definitions/ops/op_x.sqlx", config=PG), "E006")
        assert [f.line for f in found] == [3, 5]

    def test_ref_self_and_ctes_not_flagged(self):
        text = table(
            "with recent as (select * from ${ref(\"orders\")})\n"
            "select id from recent r\n"
            "join ${ref(\"customers\")} c on c.id = r.customer_id\n"
            "left join ${self()} s on s.id = r.id"
        )
        assert "E006" not in codes(lint_text(text, PATH, config=PG))

    def test_system_catalogs_and_functions_not_flagged(self):
        text = table(
            "select id from information_schema.columns\n"
            "join pg_catalog.pg_class c on true\n"
            "join public.my_func(1) f on true\n"
            "join unnest(o.tags) t on true"
        )
        assert "E006" not in codes(lint_text(text, PATH, config=PG))

    def test_allowed_schemas_config(self):
        cfg = Config(warehouse="postgres", allowed_schemas=["extensions"])
        text = table("select id from extensions.vector_index")
        assert "E006" not in codes(lint_text(text, PATH, config=cfg))
        assert "E006" in codes(lint_text(text, PATH, config=PG))

    def test_from_inside_extract_and_substring_not_flagged(self):
        text = table(
            "select id, extract(dow from d.date) as dow,\n"
            "  substring(d.code from 1 for 2) as cc, trim(both ' ' from d.name) as n\n"
            'from ${ref("dates")} d'
        )
        assert "E006" not in codes(lint_text(text, PATH, config=PG))

    def test_column_reference_after_alias_not_flagged(self):
        text = table("select o.id from ${ref(\"orders\")} o where o.id > 0")
        assert "E006" not in codes(lint_text(text, PATH, config=PG))

    def test_inline_suppression(self):
        text = table("select id from public.orders -- sqlx-lint: disable=E006 (cutover)")
        assert "E006" not in codes(lint_text(text, PATH, config=PG))


class TestE006MySQL:
    def test_backtick_two_part(self):
        text = table("select id from `shop`.`items` i")
        found = only(lint_text(text, PATH, config=MY), "E006")
        assert len(found) == 1 and "`shop`.`items`" in found[0].message

    def test_bare_two_part(self):
        text = table("select id from shop.items")
        assert "E006" in codes(lint_text(text, PATH, config=MY))


class TestE006BigQuery:
    def test_three_part_backtick_on_every_warehouse(self):
        text = table("select id from `proj.dataset.table`")
        for cfg in (PG, MY, BQ):
            assert "E006" in codes(lint_text(text, PATH, config=cfg))

    def test_two_part_backtick_shorthand(self):
        text = table("select id from `dataset.table`")
        assert "E006" in codes(lint_text(text, PATH, config=BQ))
        # On Postgres a single backtick pair is not an identifier form.
        assert "E006" not in codes(lint_text(text, PATH, config=PG))

    def test_bare_dataset_table(self):
        text = table("select id from analytics.orders")
        assert "E006" in codes(lint_text(text, PATH, config=BQ))


class TestE002Declarations:
    def test_column_types_satisfy_declaration(self):
        text = (
            'config {\n    type: "declaration",\n    schema: "raw",\n    name: "orders",\n'
            '    columnTypes: { id: "integer", amount: "numeric" }\n}\n'
        )
        assert lint_text(text, "definitions/sources/orders.sqlx", config=PG) == []

    def test_empty_column_types_still_e002(self):
        text = (
            'config {\n    type: "declaration",\n    schema: "raw",\n    name: "orders",\n'
            "    columnTypes: {}\n}\n"
        )
        assert "E002" in codes(lint_text(text, "definitions/sources/orders.sqlx"))

    def test_column_types_do_not_satisfy_tables(self):
        text = 'config {\n    type: "table",\n    columnTypes: { id: "integer" }\n}\n\nselect 1 as id\n'
        assert "E002" in codes(lint_text(text, PATH))


class TestConfigParsing:
    def test_single_quoted_type_is_read(self):
        text = "config {\n    type: 'table'\n}\n\nselect 1 as id\n"
        assert "E002" in codes(lint_text(text, PATH))

    def test_dataset_alias_for_e003(self):
        text = 'config {\n    type: "table",\n    dataset: "analytics_prod",\n    columns: { id: "Id." }\n}\n\nselect 1 as id\n'
        found = only(lint_text(text, PATH), "E003")
        assert len(found) == 1 and found[0].message.startswith('dataset: "analytics_prod"')

    def test_test_suffix_in_defaults(self):
        text = 'config {\n    type: "table",\n    schema: "analytics_test",\n    columns: { id: "Id." }\n}\n\nselect 1 as id\n'
        assert "E003" in codes(lint_text(text, PATH))

    def test_suppression_code_is_exact(self):
        text = table("select id from public.orders -- sqlx-lint: disable=E0061")
        assert "E006" in codes(lint_text(text, PATH, config=PG))


class TestS101BigQueryOnlyConfig:
    def test_bigquery_block_on_postgres(self):
        text = table(
            'select 1 as id', ',\n    bigquery: { partitionBy: "d" }'
        )
        found = only(lint_text(text, PATH, config=PG), "S101")
        assert [f.line for f in found] == [4]
        assert "bigquery:" in found[0].message

    def test_partition_by_and_cluster_by(self):
        text = table('select 1 as id', ',\n    partitionBy: "d",\n    clusterBy: ["x"]')
        found = only(lint_text(text, PATH, config=SB), "S101")
        assert len(found) == 2

    def test_silent_on_bigquery(self):
        text = table('select 1 as id', ',\n    bigquery: { partitionBy: "d" }')
        assert "S101" not in codes(lint_text(text, PATH, config=BQ))

    def test_postgres_block_is_fine(self):
        text = table(
            "select 1 as id",
            ',\n    postgres: { indexes: [{ name: "i", columns: ["id"] }] }',
        )
        assert codes(lint_text(text, PATH, config=PG)) == []


class TestS102Semicolons:
    def test_two_statements_joined_by_semicolon_in_post_operations(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  grant select on ${self()} to reader;\n"
            "  grant select on ${self()} to analyst\n}"
        )
        found = only(lint_text(text, PATH, config=PG), "S102")
        assert [f.line for f in found] == [9]

    def test_trailing_semicolon_allowed(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  grant select on ${self()} to reader;\n}"
        )
        assert "S102" not in codes(lint_text(text, PATH, config=PG))

    def test_triple_dash_separator_clean(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  grant select on ${self()} to reader\n  ---\n"
            "  grant select on ${self()} to analyst\n}"
        )
        assert "S102" not in codes(lint_text(text, PATH, config=PG))

    def test_operations_body(self):
        text = (
            'config { type: "operations" }\n\n'
            "create index if not exists i on ${ref(\"t\")} (id);\n"
            "analyze ${ref(\"t\")}\n"
        )
        found = only(lint_text(text, "definitions/ops/op_x.sqlx", config=PG), "S102")
        assert [f.line for f in found] == [3]

    def test_plpgsql_dollar_quoted_body_ignored(self):
        text = (
            'config { type: "operations" }\n\n'
            "create or replace function f() returns int language plpgsql as $$\n"
            "begin\n  return 1;\nend;\n$$;\n"
        )
        assert "S102" not in codes(lint_text(text, "definitions/ops/op_f.sqlx", config=PG))

    def test_semicolon_inside_js_template_ignored(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  ${when(!incremental(), `alter table ${self()} add primary key (id);`)}\n}",
            ctype="incremental",
        )
        assert "S102" not in codes(lint_text(text, PATH, config=PG))

    def test_semicolon_in_string_literal_ignored(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  comment on table ${self()} is 'a; b'\n}"
        )
        assert "S102" not in codes(lint_text(text, PATH, config=PG))


class TestS105UnguardedDDL:
    def test_unwrapped_primary_key_on_incremental(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  ALTER TABLE ${self()} ADD PRIMARY KEY (id)\n}",
            ctype="incremental",
        )
        found = only(lint_text(text, PATH, config=PG), "S105")
        assert [f.line for f in found] == [9]

    def test_wrapped_primary_key_is_clean(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  ${when(!incremental(), `ALTER TABLE ${self()} ADD PRIMARY KEY (id)`)}\n}",
            ctype="incremental",
        )
        assert "S105" not in codes(lint_text(text, PATH, config=PG))

    def test_when_incremental_without_negation_still_flagged(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  ${when(incremental(), `ALTER TABLE ${self()} ADD CONSTRAINT c UNIQUE (id)`)}\n}",
            ctype="incremental",
        )
        assert "S105" in codes(lint_text(text, PATH, config=PG))

    def test_full_refresh_table_not_flagged(self):
        text = table(
            "select 1 as id\n\npost_operations {\n"
            "  ALTER TABLE ${self()} ADD PRIMARY KEY (id)\n}"
        )
        assert "S105" not in codes(lint_text(text, PATH, config=PG))


class TestConfigShapeRules:
    def test_s103_string_index_method(self):
        text = table(
            "select 1 as id",
            ',\n    postgres: {\n      indexes: [{ name: "i", columns: ["id"], method: "btree" }]\n    }',
        )
        found = only(lint_text(text, PATH, config=PG), "S103")
        assert [f.line for f in found] == [5] and '"btree"' in found[0].message

    def test_s103_numeric_method_clean(self):
        text = table(
            "select 1 as id",
            ',\n    postgres: { indexes: [{ name: "i", columns: ["id"], method: 2 }] }',
        )
        assert "S103" not in codes(lint_text(text, PATH, config=PG))

    def test_s104_incremental_strategy_on_postgres(self):
        text = table(
            "select 1 as id", ',\n    incrementalStrategy: "insert_overwrite"', ctype="incremental"
        )
        assert "S104" in codes(lint_text(text, PATH, config=PG))
        assert "S104" not in codes(lint_text(text, PATH, config=BQ))

    def test_s106_both_unique_keys(self):
        text = table(
            "select 1 as id",
            ',\n    assertions: { uniqueKey: ["id"], uniqueKeys: [["id"]] }',
        )
        assert "S106" in codes(lint_text(text, PATH, config=PG))

    def test_s106_one_unique_key_clean(self):
        text = table("select 1 as id", ',\n    assertions: { uniqueKey: ["id"], nonNull: ["id"] }')
        assert "S106" not in codes(lint_text(text, PATH, config=PG))

    def test_top_level_unique_key_not_confused(self):
        text = table(
            "select 1 as id",
            ',\n    uniqueKey: ["id"],\n    assertions: { uniqueKeys: [["id"]] }',
            ctype="incremental",
        )
        assert "S106" not in codes(lint_text(text, PATH, config=PG))

    def test_s108_jit_code(self):
        text = "config { type: \"table\", columns: { id: \"Id.\" } }\n\n${self().jitCode()}\nselect 1 as id\n"
        found = only(lint_text(text, PATH, config=PG), "S108")
        assert [f.line for f in found] == [3]

    def test_rules_can_be_disabled(self):
        cfg = Config(warehouse="postgres", disabled={"S101"})
        text = table('select 1 as id', ',\n    partitionBy: "d"')
        assert "S101" not in codes(lint_text(text, PATH, config=cfg))
