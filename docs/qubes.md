# Qubes documentation

## Build an RPM package

You can build an RPM package for Qubes using the following commands:

```
podman build -t dz-rpm-builder qubes/
podman run --rm -v .:/root/dangerzone-image dz-rpm-builder
```

The RPM package will be stored under `./qubes/dist`.
