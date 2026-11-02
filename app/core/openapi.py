"""Bantuan dokumentasi OpenAPI untuk endpoint yang membaca body secara manual (JSON atau multipart)."""

from pydantic import BaseModel


def _inline(node, defs: dict):
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if ref.startswith("#/$defs/"):
            return _inline(defs[ref.removeprefix("#/$defs/")], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def inline_json_schema(model: type[BaseModel]) -> dict:
    """Skema JSON model tanpa $defs internal, supaya valid di dokumen OpenAPI."""
    schema = model.model_json_schema()
    return _inline(schema, schema.get("$defs", {}))


def invitation_request_body(model: type[BaseModel], *, with_role: bool) -> dict:
    form_properties: dict = {
        "file": {"type": "string", "format": "binary", "description": "CSV kolom email,name,department,role"},
        "department": {"type": "string"},
    }
    if with_role:
        form_properties["role"] = {"type": "string", "enum": ["worker", "manager", "hr_admin"]}
    return {
        "requestBody": {
            "content": {
                "application/json": {"schema": inline_json_schema(model)},
                "multipart/form-data": {
                    "schema": {"type": "object", "properties": form_properties, "required": ["file"]}
                },
            },
            "required": True,
        }
    }
