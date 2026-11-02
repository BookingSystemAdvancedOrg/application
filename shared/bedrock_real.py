"""The real bedrock client, a skeleton until feature A (meta-ads) builds it:
every method is the port's and raises NotImplementedError, so
BEDROCK_CLIENT=real fails loudly instead of pretending to work."""

from shared.bedrock import TextModel


class RealTextModel(TextModel):
    pass
