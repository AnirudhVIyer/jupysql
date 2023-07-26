from typing import Iterator, Iterable
from collections.abc import MutableMapping
from jinja2 import Template
from ploomber_core.exceptions import modify_exceptions
import sql.connection
import difflib
import glob
import os
from sql import exceptions
from sql import query_util
from pathlib import Path
from sql import display

SNIPPETS_DIR = "jupysql-snippets/"


class SQLStore(MutableMapping):
    """Stores SQL scripts to render large queries with CTEs

    Notes
    -----
    .. versionadded:: 0.4.3

    Examples
    --------
    >>> from sql.store import SQLStore
    >>> sqlstore = SQLStore()
    >>> sqlstore.store("writers_fav",
    ...                "SELECT * FROM writers WHERE genre = 'non-fiction'")
    >>> sqlstore.store("writers_fav_modern",
    ...                "SELECT * FROM writers_fav WHERE born >= 1970",
    ...                with_=["writers_fav"])
    >>> query = sqlstore.render("SELECT * FROM writers_fav_modern LIMIT 10",
    ...                         with_=["writers_fav_modern"])
    >>> print(query)
    WITH "writers_fav" AS (
        SELECT * FROM writers WHERE genre = 'non-fiction'
    ), "writers_fav_modern" AS (
        SELECT * FROM writers_fav WHERE born >= 1970
    )
    SELECT * FROM writers_fav_modern LIMIT 10
    """

    def __init__(self):
        self._data = dict()

    def __setitem__(self, key: str, value: str) -> None:
        self._data[key] = value

    def __getitem__(self, key) -> str:
        if not self._data:
            raise exceptions.UsageError("No saved SQL")
        if key not in self._data:
            matches = difflib.get_close_matches(key, self._data)
            error = f'"{key}" is not a valid snippet identifier.'
            if matches:
                raise exceptions.UsageError(error + f' Did you mean "{matches[0]}"?')
            else:
                valid = ", ".join(f'"{key}"' for key in self._data.keys())
                raise exceptions.UsageError(error + f" Valid identifiers are {valid}.")
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        for key in self._data:
            yield key

    def __len__(self) -> int:
        return len(self._data)

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def render(self, query, with_=None):
        # TODO: if with is false, WITH should not appear
        return SQLQuery(self, query, with_)

    def infer_dependencies(self, query, key):
        dependencies = []
        saved_keys = [
            saved_key for saved_key in list(self._data.keys()) if saved_key != key
        ]
        if saved_keys and query:
            tables = query_util.extract_tables_from_query(query)
            for table in tables:
                if table in saved_keys:
                    dependencies.append(table)
        return dependencies

    @modify_exceptions
    def store(self, key, query, with_=None):
        if "-" in key:
            raise exceptions.UsageError(
                "Using hyphens (-) in save argument isn't allowed."
                " Please use underscores (_) instead"
            )
        if with_ and key in with_:
            raise exceptions.UsageError(
                f"Script name ({key!r}) cannot appear in with_ argument"
            )

        self._data[key] = SQLQuery(self, query, with_)


class SQLQuery:
    """Holds queries and renders them"""

    def __init__(self, store: SQLStore, query: str, with_: Iterable = None):
        self._store = store
        self._query = query
        self._with_ = with_ or []

        if any("-" in x for x in self._with_):
            raise exceptions.UsageError(
                "Using hyphens is not allowed. "
                "Please use "
                + ", ".join(self._with_).replace("-", "_")
                + " instead for the with argument.",
            )

    def __str__(self) -> str:
        """
        We use the ' (backtick symbol) to wrap the CTE alias if the dialect supports
        ` (backtick)
        """
        with_clause_template = Template(
            """WITH{% for name in with_ %} {{name}} AS ({{rts(saved[name]._query)}})\
{{ "," if not loop.last }}{% endfor %}{{query}}"""
        )
        with_clause_template_backtick = Template(
            """WITH{% for name in with_ %} `{{name}}` AS ({{rts(saved[name]._query)}})\
{{ "," if not loop.last }}{% endfor %}{{query}}"""
        )
        is_use_backtick = (
            sql.connection.ConnectionManager.current.is_use_backtick_template()
        )
        with_all = _get_dependencies(self._store, self._with_)
        template = (
            with_clause_template_backtick if is_use_backtick else with_clause_template
        )
        # return query without 'with' when no dependency exists
        if len(with_all) == 0:
            return self._query.strip()
        return template.render(
            query=self._query,
            saved=self._store._data,
            with_=with_all,
            rts=_remove_trailing_semicolon,
        )


def _remove_trailing_semicolon(query):
    query_ = query.rstrip()
    return query_[:-1] if query_[-1] == ";" else query


def _get_dependencies(store, keys):
    """Get a list of all dependencies to reconstruct the CTEs in keys"""
    # get the dependencies for each key
    deps = _flatten([_get_dependencies_for_key(store, key) for key in keys])
    # remove duplicates but preserve order
    return list(dict.fromkeys(deps + keys))


def _get_dependents_for_key(store, key):
    key_dependents = []
    for k in list(store):
        deps = _get_dependencies_for_key(store, k)
        if key in deps:
            key_dependents.append(k)
    return key_dependents


def _get_dependencies_for_key(store, key):
    """Retrieve dependencies for a single key"""
    deps = store[key]._with_
    deps_of_deps = _flatten([_get_dependencies_for_key(store, dep) for dep in deps])
    return deps_of_deps + deps


def _flatten(elements):
    """Flatten a list of lists"""
    return [element for sub in elements for element in sub]


def store_snippet_as_sql(sql_command, snippet_name):
    """
    Store snippet as a .sql file

    Parameters
    ----------
    command : str
        query to be saved as the snippet .

    snippet_name : str
        Name of the saved snippet
    """

    snippet_path = Path(SNIPPETS_DIR) / f"{snippet_name}.sql"
    snippet_path.parent.mkdir(parents=True, exist_ok=True)
    with open(snippet_path, "w") as file:
        file.write(sql_command)
    message = """Manual editing of .sql files may not be reflected when
    reopening the notebook. Please edit snippets directly in the notebook
    to ensure consistency."""

    display.message(message, style="font-size: 12px; font-style: italic;")


def load_snippet_from_sql(store):
    """
    Load the snippets saved in .sql files
    as snippets in SQLStore.

    Parameters
    ----------
    store : SQLStore
        SQLStore of the current session .

    """
    if os.path.exists(SNIPPETS_DIR) and os.path.isdir(SNIPPETS_DIR):
        snippet_files = glob.glob(SNIPPETS_DIR + "*.sql")
        snippet_names = [filename[len(SNIPPETS_DIR) : -4] for filename in snippet_files]
        for name, filename in zip(snippet_names, snippet_files):
            with open(filename, "r") as file:
                snippet_content = file.read()
                key = query_util.extract_tables_from_query(snippet_content)
                dependencies = store.infer_dependencies(snippet_content, key=key)
                store.store(name, snippet_content, with_=dependencies)


# session-wide store
store = SQLStore()
