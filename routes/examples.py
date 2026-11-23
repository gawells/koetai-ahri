"""SPARQL examples — store, browse, run (sparql-examples RDF format)."""
import json
import re
from flask import (Blueprint, render_template, request, jsonify,
                   redirect, url_for, flash)
from flask_login import login_required, current_user
import config
from services.db import get_db
from services import triplestore

bp = Blueprint("examples", __name__, url_prefix="/u")


def _get_dataset(owner_orcid, slug):
    db = get_db()
    return db.execute(
        "SELECT d.*, u.orcid_id FROM datasets d JOIN users u ON u.id = d.user_id "
        "WHERE u.orcid_id = ? AND d.slug = ?",
        (owner_orcid, slug)
    ).fetchone()


@bp.route("/<owner_orcid>/<slug>/examples")
def list_examples(owner_orcid, slug):
    ds = _get_dataset(owner_orcid, slug)
    if not ds:
        flash("Dataset not found.", "error")
        return redirect(url_for("index"))

    db = get_db()
    examples = db.execute(
        "SELECT * FROM examples WHERE dataset_id = ? ORDER BY label",
        (ds["id"],)
    ).fetchall()
    return render_template("examples.html", ds=ds, examples=examples)


@bp.route("/<owner_orcid>/<slug>/examples/list")
def list_examples_json(owner_orcid, slug):
    """JSON list for inline loading in the SPARQL editor."""
    ds = _get_dataset(owner_orcid, slug)
    if not ds:
        return jsonify([])
    examples = get_db().execute(
        "SELECT slug, label, description, query, keywords FROM examples WHERE dataset_id=? ORDER BY label",
        (ds["id"],)
    ).fetchall()
    return jsonify([dict(e) for e in examples])


def _owned_dataset(owner_orcid, slug):
    """The dataset, if the current user owns it; None otherwise."""
    ds = _get_dataset(owner_orcid, slug)
    if not ds or not current_user.is_authenticated or ds["user_id"] != current_user.id:
        return None
    return ds


def _example_fields():
    """Label, description, query and keywords (as a JSON array string) from
    either a form post (the example pages) or a JSON body (the SPARQL tab).

    A form gives keywords as one comma-separated string; JSON may give either
    that or a list.
    """
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    kw = data.get("keywords", "")
    if isinstance(kw, str):
        kw = kw.split(",")
    keywords = json.dumps([str(k).strip() for k in kw if str(k).strip()])
    return {
        "label":       str(data.get("label", "")).strip(),
        "description": str(data.get("description", "") or "").strip(),
        "query":       str(data.get("query", "")).strip(),
        "keywords":    keywords,
        "slug":        str(data.get("slug", "")).strip().lower().replace(" ", "-"),
    }


def _done(ok, message, owner_orcid, slug, status=200, **extra):
    """Answer a JSON caller with JSON, a form caller with a flash and redirect."""
    if request.is_json:
        return jsonify({"success": ok, "message": message, **extra}), status
    flash(message, "success" if ok else "error")
    return redirect(url_for("examples.list_examples", owner_orcid=owner_orcid, slug=slug))


@bp.route("/<owner_orcid>/<slug>/examples/new", methods=["GET", "POST"])
@login_required
def new_example(owner_orcid, slug):
    ds = _owned_dataset(owner_orcid, slug)
    if not ds:
        if request.is_json:
            return jsonify({"success": False, "message": "Not authorized."}), 403
        flash("Not authorized.", "error")
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        f = _example_fields()
        if not (f["label"] and f["slug"] and f["query"]):
            return _done(False, "Label, slug and query are required.", owner_orcid, slug, 400)
        if not _SLUG_RE.fullmatch(f["slug"]):
            return _done(False, "Slug may contain only lowercase letters, digits and hyphens.",
                         owner_orcid, slug, 400)

        db = get_db()
        if db.execute("SELECT 1 FROM examples WHERE dataset_id=? AND slug=?",
                      (ds["id"], f["slug"])).fetchone():
            return _done(False, f"An example with slug '{f['slug']}' already exists.",
                         owner_orcid, slug, 409)
        db.execute(
            "INSERT INTO examples (dataset_id, slug, label, description, query, keywords) VALUES (?,?,?,?,?,?)",
            (ds["id"], f["slug"], f["label"], f["description"], f["query"], f["keywords"])
        )
        # The RDF copy in the :examples graph goes in before the row is
        # committed, so a failed update does not leave the two disagreeing.
        ok, msg = _store_example_rdf(ds, f["slug"], f["label"], f["description"],
                                     f["query"], f["keywords"])
        if not ok:
            db.rollback()
            return _done(False, f"Could not save the example to the triplestore: {msg}",
                         owner_orcid, slug, 502)
        db.commit()
        return _done(True, f"Example '{f['label']}' saved.", owner_orcid, slug, example_slug=f["slug"])

    return render_template("example_edit.html", ds=ds, example=None)


@bp.route("/<owner_orcid>/<slug>/examples/<ex_slug>/edit", methods=["GET", "POST"])
@login_required
def edit_example(owner_orcid, slug, ex_slug):
    """Change an example in place.

    The slug is not editable: it is the last segment of the example's IRI
    (<graph_base>/examples/<slug>), so changing it would change a published
    identifier. A different slug is a new example.
    """
    ds = _owned_dataset(owner_orcid, slug)
    if not ds:
        if request.is_json:
            return jsonify({"success": False, "message": "Not authorized."}), 403
        flash("Not authorized.", "error")
        return redirect(url_for("dashboard.index"))

    db = get_db()
    ex = db.execute("SELECT * FROM examples WHERE dataset_id=? AND slug=?",
                    (ds["id"], ex_slug)).fetchone()
    if not ex:
        return _done(False, "Example not found.", owner_orcid, slug, 404)

    if request.method == "POST":
        f = _example_fields()
        if not (f["label"] and f["query"]):
            return _done(False, "Label and query are required.", owner_orcid, slug, 400)
        db.execute(
            "UPDATE examples SET label=?, description=?, query=?, keywords=? WHERE id=?",
            (f["label"], f["description"], f["query"], f["keywords"], ex["id"])
        )
        ok, msg = _store_example_rdf(ds, ex_slug, f["label"], f["description"],
                                     f["query"], f["keywords"], replace=True)
        if not ok:
            db.rollback()
            return _done(False, f"Could not update the example in the triplestore: {msg}",
                         owner_orcid, slug, 502)
        db.commit()
        return _done(True, f"Example '{f['label']}' updated.", owner_orcid, slug, example_slug=ex_slug)

    return render_template("example_edit.html", ds=ds, example=ex)


@bp.route("/<owner_orcid>/<slug>/examples/<ex_slug>/run", methods=["POST"])
def run_example(owner_orcid, slug, ex_slug):
    ds = _get_dataset(owner_orcid, slug)
    if not ds:
        return jsonify({"error": "Dataset not found"}), 404

    db = get_db()
    ex = db.execute(
        "SELECT * FROM examples WHERE dataset_id = ? AND slug = ?",
        (ds["id"], ex_slug)
    ).fetchone()
    if not ex:
        return jsonify({"error": "Example not found"}), 404

    ok, result = triplestore.get(ds).sparql_query(
        ex["query"], graphs=triplestore.dataset_scope(ds))
    if not ok:
        return jsonify(result), 500
    return jsonify(result)


@bp.route("/<owner_orcid>/<slug>/examples/<ex_slug>/delete", methods=["POST"])
@login_required
def delete_example(owner_orcid, slug, ex_slug):
    ds = _get_dataset(owner_orcid, slug)
    if not ds or ds["user_id"] != current_user.id:
        return jsonify({"error": "Not authorized"}), 403

    db = get_db()
    db.execute(
        "DELETE FROM examples WHERE dataset_id = ? AND slug = ?",
        (ds["id"], ex_slug)
    )
    # The RDF copy too: deleting only the row left the example in the
    # :examples graph, where SPARQL still found it.
    ok, msg = triplestore.get(ds).sparql_update(
        f"DELETE WHERE {{ GRAPH <{_examples_graph(ds)}> {{ <{_example_iri(ds, ex_slug)}> ?p ?o }} }}")
    if not ok:
        db.rollback()
        return jsonify({"error": f"Could not delete the example from the triplestore: {msg}"}), 502
    db.commit()
    return jsonify({"success": True})


_SLUG_RE = re.compile(r"[a-z0-9-]+")


def _literal(s):
    """A SPARQL string literal, quotes included.

    Every value written into the update goes through here: an unescaped quote
    or newline in a label, description or keyword ends the literal early and
    the whole update fails (or worse, parses as something else).
    """
    s = str(s or "")
    for a, b in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"),
                 ("\r", "\\r"), ("\t", "\\t")):
        s = s.replace(a, b)
    return f'"{s}"'


def _examples_graph(ds):
    return ds["graph_base"] + "/examples"


def _example_iri(ds, ex_slug):
    return f"{ds['graph_base']}/examples/{ex_slug}"


def _store_example_rdf(ds, ex_slug, label, description, query, keywords_json,
                       replace=False):
    """Store example as RDF (schema:target / sib sparql-examples format) in the
    dataset's own triplestore.

    With replace=True the example's existing triples are removed in the same
    update request, so an edit never leaves the old and new versions side by
    side (and never leaves neither).
    """
    ex_uri       = _example_iri(ds, ex_slug)
    endpoint_uri = f"{config.BASE_URL}/u/{ds['orcid_id']}/{ds['slug']}/sparql"
    keywords_list = json.loads(keywords_json or "[]")
    kw_triples  = "\n".join(f"  schema:keywords {_literal(kw)} ;" for kw in keywords_list)
    graph_uri = _examples_graph(ds)

    ttl = f"""
<{ex_uri}> a schema:SoftwareSourceCode ;
  schema:name {_literal(label)} ;
  rdfs:comment {_literal(description)} ;
  schema:target <{endpoint_uri}> ;
  sh:select {_literal(query)} ;
{kw_triples}
  .
"""
    update = f"""
PREFIX schema: <https://schema.org/>
PREFIX sh:     <http://www.w3.org/ns/shacl#>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
"""
    if replace:
        update += f"DELETE WHERE {{ GRAPH <{graph_uri}> {{ <{ex_uri}> ?p ?o }} }} ;\n"
    update += f"""INSERT DATA {{
  GRAPH <{graph_uri}> {{
    {ttl}
  }}
}}"""
    return triplestore.get(ds).sparql_update(update)
