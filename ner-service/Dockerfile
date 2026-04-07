FROM python:3.12-slim

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
