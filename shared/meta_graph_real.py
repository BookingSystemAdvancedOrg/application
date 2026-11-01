"""The real meta_graph client, a skeleton until feature A (meta-ads) builds it:
every method is the port's and raises NotImplementedError, so
META_CLIENT=real fails loudly instead of pretending to work."""

from shared.meta_graph import MetaGraph


class RealMetaGraph(MetaGraph):
    pass
