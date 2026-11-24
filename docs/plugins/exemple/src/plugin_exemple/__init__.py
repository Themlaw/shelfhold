"""Plugin d'exemple de Shelfhold (source synthetique, hors ligne, MIT)."""

from .source import KEY, build_manifest, ExampleSource

__all__ = ["KEY", "ExampleSource", "build_manifest"]