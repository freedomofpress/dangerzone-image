FROM fedora:43

RUN dnf install -y rpm-build python3 python3-devel pyproject-rpm-macros \
    python3-uv-build python3-pymupdf python3-magic

CMD ["/root/dangerzone-image/qubes/build-rpm.sh"]
