# Dangerzone-image

This repository contains the dangerzone container image that is used to perform "document to pixels" conversions. This container is used by [dangerzone](https://dangerzone.rocks) to securely converst its documents.

## Using the container image

The image is published on a monthly basis on the container registry, alongside their Cosign signatures.

| Channel | Location                               | Signed?    | Use it for  |
| ------- | -------------------------------------- | ---------- | ----------- |
| Stable  | `ghcr.io/freedomofpress/dangerzone/v1` | ✅ (keys)  | Production  |

Additionally, nightly and branches are published on different locations. It can be useful to use these when implementing new features, or for experimenting.

| Channel | Location                                            | Signed?    | Use it for  |
| ------- | --------------------------------------------------- | ---------- | ----------- |
| Nightly | `ghcr.io/freedomofpress/dangerzone-testing/main/v1` | ✅ (keys)  | Development |
| Branch  | `ghcr.io/freedomofpress/dangerzone-testing/<branch-name>/v1` | ✅ (keys)  | Development |


## dangerzone-insecure-conversion python package

> [!WARNING]
> Do not use this unless you are certain about what you are doing.
> Do not use this to convert documents that should be processed safely!

The python code that runs inside the container is packaged under the name "dangerzone-insecure-conversion". It's considered insecure because the intended way to run dangerzone is by using a hardened sandbox, which is provided by dangerzone.

With that being said, there are situations where it's useful to run this code on its own, for instance when adding new file formats.

### Running the tests

```bash
uv pip install -e .
uv run pytest
```