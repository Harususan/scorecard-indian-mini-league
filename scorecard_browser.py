"""A small Streamlit component for per-browser captain preferences."""
from pathlib import Path
import streamlit.components.v1 as components

_picker = components.declare_component("iml_captain_picker", path=str(Path(__file__).parent / "captain_picker"))


def captain_picker(teams, namespace):
    clubs = [{"name":name,"managers":[{"id":str(m["id"]),"manager":m["manager"]} for m in team["managers"]]} for name,team in sorted(teams.items())]
    return _picker(clubs=clubs, namespace=namespace, key=f"captain-picker:{namespace}", default=None)


def validate_choices(result, teams, namespace):
    if not isinstance(result, dict) or result.get("namespace") != namespace or not result.get("ready"):
        return None
    saved = result.get("choices")
    if not isinstance(saved, dict): return None
    choices = {}
    for name, team in teams.items():
        selected = saved.get(name)
        allowed = {str(m["id"]) for m in team["managers"]}
        choices[name] = selected if isinstance(selected, str) and selected in allowed else None
    return choices
