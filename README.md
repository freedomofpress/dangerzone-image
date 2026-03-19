# Dangerzone-image

This repository contains the dangerzone container image that is used to perform "document to pixels" conversions. This container is used by [dangerzone](https://dangerzone.rocks) to securely converst its documents.

## Using the container image

The image is published on a monthly basis on the container registry, alongside their Cosign signatures. 
Additionally, nightly and development branches are published under the `dangerzone-testing` namespace.

| Channel | Location                               | Signed?    | Use it for  |
| ------- | -------------------------------------- | ---------- | ----------- |
| Stable  | `ghcr.io/freedomofpress/dangerzone/v1` | ✅ ([prod keys](/freedomofpress-dangerzone.pub))  | Production  |
| Nightly | `ghcr.io/freedomofpress/dangerzone-testing/main/v1` | ✅ ([testing keys](/tests/assets/dangerzone-testing.pub))  | Development |
| Branch  | `ghcr.io/freedomofpress/dangerzone-testing/<branch-name>/v1` | ✅ ([testing keys](/tests/assets/dangerzone-testing.pub))  | Development |

## What this container provides

This container provides a way to convert documents to pixel buffers inside a secure sandbox. The security of the sandbox is provided by different layers:

- The container is [reproducible](/docs/reproducibility.md) 
- [gVisor](/docs/gvisor.md)


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

# Or, if you prefer to run the tests outside the sandbox:
uv run pytest --local
```
