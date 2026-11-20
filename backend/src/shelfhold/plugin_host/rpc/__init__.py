"""The wire between the core and an isolated plugin (`rpc_protocol` 1, D12, EF-SRC-016).

Length-prefixed JSON-RPC 2.0: a protocol of its own, apart from the data contract (`api`) and from
the local CLI socket (`infrastructure/local_socket`), with which it shares nothing but the fact
that both speak JSON. `codec` and `protocol` are common to both ends; `host_side` runs in the core,
`plugin_side` in the plugin's subprocess and imports nothing of the core.
"""
