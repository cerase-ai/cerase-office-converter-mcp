# Cerase Office Converter MCP — cross-format document conversion via
# pandoc + LibreOffice headless + TeX Live.
#
# Exposed tools (one per format pair, with a generic fallback):
#   convert_docx_to_pdf, convert_docx_to_odt, convert_odt_to_docx,
#   convert_pptx_to_pdf, convert_pptx_to_odp, convert_odp_to_pptx,
#   convert_xlsx_to_pdf, convert_xlsx_to_ods, convert_ods_to_xlsx,
#   convert_md_to_pdf (pandoc + TeX), convert_md_to_docx (pandoc),
#   convert(input_b64, source_format, target_format) — catch-all
#
# Distribution: built locally from this Dockerfile. Image is large
# (~1.5GB) because LibreOffice + TeX Live ship full Java + Java fonts
# + Office templates. Acceptable: 1 container per appliance, used
# only during conversion bursts (not continuously).
#
# Pattern mirrors cerase-deck-renderer-mcp (same FastMCP server +
# mcp-proxy stdio→HTTP bridge contract).
FROM python:3.13.9-slim@sha256:326df678c20c78d465db501563f3492d17c42a4afe33a1f2bf5406a1d56b0e86

# System deps:
# - libreoffice (headless office suite, the workhorse for binary↔ODF↔PDF)
# - libreoffice-java-common + default-jre-headless (LibreOffice macros / scripts)
# - pandoc (markdown ↔ docx / odt / html / many)
# - texlive-xetex + texlive-latex-recommended + texlive-fonts-recommended
#   (PDF generation engine for pandoc; xelatex handles unicode + EU fonts)
# - fonts-* baseline (so converted docs don't render with missing-glyph squares)
# - ca-certificates, curl (diagnostics + https for mcp-proxy)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        libreoffice-java-common \
        default-jre-headless \
        pandoc \
        texlive-xetex \
        texlive-latex-recommended \
        texlive-fonts-recommended \
        fonts-liberation \
        fonts-dejavu \
        fonts-noto-core \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Python toolchain: mcp + mcp-proxy for the stdio→HTTP bridge.
# Pinned via requirements.txt (OPT-15).
COPY requirements.txt requirements.lock /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm /tmp/requirements.txt /tmp/requirements.lock

# MCP server skeleton.
COPY server.py /app/server.py

# OPT-14: non-root runtime user. LibreOffice needs a writable $HOME
# for its user profile (server.py points UserInstallation= at
# /tmp/cerase-office-profile, but soffice still touches $HOME on
# first run for fontconfig / icc cache). Setting HOME explicitly so
# it lands somewhere the appuser owns.
RUN groupadd -r appuser \
 && useradd -r -g appuser -u 1000 -m -d /home/appuser -s /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
ENV HOME=/home/appuser
USER appuser
WORKDIR /home/appuser

EXPOSE 3000

# mcp-proxy bridges stdio MCP → HTTP /sse on port 3000.
# M-CI-3: image-level liveness — runtime-spawned MCP containers have no
# compose healthcheck, this is the only signal `docker ps`/doctor sees.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 3000), timeout=5)" || exit 1

ENTRYPOINT ["sh", "-c", "exec mcp-proxy --port 3000 --host 0.0.0.0 --pass-environment -- python /app/server.py"]
