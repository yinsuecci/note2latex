FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-xetex texlive-lang-chinese texlive-pictures texlive-latex-recommended \
    && rm -rf /var/lib/apt/lists/*
ENV openin_any=p openout_any=p TEXMFVAR=/tmp/texmf-var TEXMFCONFIG=/tmp/texmf-config
WORKDIR /work
