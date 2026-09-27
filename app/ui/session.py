"""Bind callback authorization to Gradio's server-side per-browser State."""

from contextvars import ContextVar
from functools import wraps
from inspect import Parameter, signature

current_session = ContextVar("ui_session", default=None)


def api_headers():
    session = current_session.get()
    if not session or not session.get("access_token"):
        raise ValueError("Sign in before using workspace features.")
    return {"Authorization": "Bearer " + session["access_token"],
            "X-Workspace-ID": session["workspace_id"]}


def bind_session(function):
    """Append one Gradio State input; nested callbacks inherit the context."""
    original = signature(function)
    count = len(original.parameters)

    @wraps(function)
    async def callback(*args, **kwargs):
        session = kwargs.pop("session", None)
        supplied = len(args) > count or session is not None
        if len(args) > count:
            *args, session = args
        marker = current_session.set(session) if supplied else None
        try:
            return await function(*args, **kwargs)
        finally:
            if marker is not None:
                current_session.reset(marker)

    callback.__signature__ = original.replace(parameters=[*original.parameters.values(),
        Parameter("session", Parameter.POSITIONAL_OR_KEYWORD, default=None)])
    return callback
