# Pinned by digest so the build is reproducible and can't be swapped under the
# tag. Digest is the multi-arch index for python:3.12-slim (Docker resolves the
# right arch from it). To refresh: docker manifest inspect python:3.12-slim, or
#   curl -sI -H "Authorization: Bearer $(curl -s 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')" \
#     -H 'Accept: application/vnd.oci.image.index.v1+json' \
#     https://registry-1.docker.io/v2/library/python/manifests/3.12-slim | grep -i docker-content-digest
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

ARG SPACY_MODEL=en_core_web_sm

RUN pip install --no-cache-dir \
    fastapi==0.115.14 \
    uvicorn==0.34.3 \
    presidio-analyzer==2.2.362 \
    presidio-anonymizer==2.2.362 \
    spacy==3.7.5

RUN python -m spacy download ${SPACY_MODEL}

COPY app.py .

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
