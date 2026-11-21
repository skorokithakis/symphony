"""Web UI for the built-in tracker backend.

:class:`WebUIServer` is the HTTP entry point.  :mod:`symphony_linear.webui.routes`
is the route table, and :mod:`symphony_linear.webui.server` is the single
request pipeline every route goes through.
"""

from symphony_linear.webui.server import WebUIServer

__all__ = ["WebUIServer"]
