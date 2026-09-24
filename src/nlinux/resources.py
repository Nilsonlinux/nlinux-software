import os

# Resolves packaged resources (apps index, assets, translations) relative to
# the source tree, avoiding a dependency on the deprecated pkg_resources module.
SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def resource_path(resource: str) -> str:
    return os.path.join(SRC_ROOT, resource)


def resource_exists(resource: str) -> bool:
    return os.path.exists(resource_path(resource))