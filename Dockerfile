# --- Etage 1 : compilation de l'interface -----------------------------------
FROM node:22-alpine AS ui

WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci || npm install
COPY ui/ ./
RUN npm run build


# --- Etage 2 : execution ------------------------------------------------------
FROM python:3.12-slim AS runtime

# L'identite n'est PAS figee ici. Elle doit correspondre au proprietaire des
# fichiers a deplacer, que seul l'utilisateur connait : un UID choisi a la
# construction ne vaut que pour qui reconstruit l'image, et quiconque tire
# l'image publiee heritait de 1000:1000. C'est l'entrypoint qui l'ajuste au
# demarrage, a partir de PUID/PGID, puis abandonne les droits root.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffprobe lit la duree, la resolution reelle et les tags des conteneurs. C'est
# ce qui permet de contredire un nom de release menteur — meme approche que
# Plex. On installe ffmpeg pour ffprobe seul ; le code degrade proprement si le
# binaire est absent, mais on ne veut pas de cette degradation par defaut.
# gosu permet d'abandonner les droits root en transmettant les signaux, ce que
# « su » ne fait pas correctement — un conteneur doit pouvoir s'arreter.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sortilege/ ./sortilege/

# L'extra "ai" est inclus : la dependance est legere et le resolveur reste
# desactive tant que SORTILEGE_AI_ENABLED n'est pas a true.
RUN pip install --no-cache-dir ".[ai]"

# Interface compilee, servie en statique par FastAPI (pas de second port).
COPY --from=ui /ui/dist ./sortilege/web/static

RUN mkdir -p /app/data

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8117

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8117/api/health', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "sortilege.main:app", "--host", "0.0.0.0", "--port", "8117"]
