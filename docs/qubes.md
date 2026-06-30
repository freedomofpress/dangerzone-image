# Qubes documentation

## Build an RPM package

### From a Fedora environment

Install some dependencies first:

```
sudo dnf install -y python3-uv-build python3-pymupdf python3-magic
```

Build the RPM package:

```
./qubes/build-rpm.sh
```

The RPM package will be stored under `./qubes/dist`.

### From a container

Install Podman (or Docker, and adjust accordingly):

```
sudo dnf install -y podman
```

Build the `dz-rpm-builder` image, that contains the tools to build the Qubes
RPM:

```
podman build -t dz-rpm-builder qubes/
```

Build the RPM package:

```
podman run --rm -v .:/root/dangerzone-image dz-rpm-builder
```

The RPM package will be stored under `./qubes/dist`.
