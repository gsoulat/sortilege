# --- Etage 1 : compilation de l'interface -----------------------------------
FROM node:22-alpine AS ui

WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci || npm install
COPY ui/ ./
RUN npm run build


# --- Etage 2 : execution ------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root : l'application ecrit dans la bibliotheque de l'utilisateur, elle n'a
# aucune raison d'etre root. UID/GID alignables sur ceux du NAS au build.
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffprobe lit la duree, la resolution reelle et les tags des conteneurs. C'est
# ce qui permet de contredire un nom de release menteur — meme approche que
# Plex. On installe ffmpeg pour ffprobe seul ; le code degrade proprement si le
# binaire est absent, mais on ne veut pas de cette degradation par defaut.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${GID}" sortilege \
 && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin sortilege

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sortilege/ ./sortilege/

# L'extra "ai" est inclus : la dependance est legere et le resolveur reste
# desactive tant que SORTILEGE_AI_ENABLED n'est pas a true.
RUN pip install --no-cache-dir ".[ai]"

# Interface compilee, servie en statique par FastAPI (pas de second port).
COPY --from=ui /ui/dist ./sortilege/web/static

RUN mkdir -p /app/data && chown -R sortilege:sortilege /app

USER sortilege

EXPOSE 8117

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8117/api/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "sortilege.main:app", "--host", "0.0.0.0", "--port", "8117"]
