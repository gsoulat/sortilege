"""Configuration, entierement pilotee par l'environnement.

Aucune valeur secrete n'a de defaut utilisable : une installation mal
configuree doit refuser de demarrer, pas tourner en mode degrade silencieux.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .core.scoring import AUTO_APPLY_THRESHOLD, REJECT_THRESHOLD


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SORTILEGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Chemins ---
    # NoDecode est indispensable : sans lui, pydantic-settings tente de decoder
    # tout champ de type complexe (ici une liste) comme du JSON AVANT d'appeler
    # le validateur, et « /a:/b » fait exploser json.loads.
    source_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    library_root: Path = Path("/storage/media")

    allowed_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    """Zones que l'interface peut PARCOURIR, au-dela des sources declarees.

    Distinct des sources a dessein : un NAS monte souvent un volume entier
    (« /volume1:/storage ») dont seuls quelques dossiers servent de source.
    Sans cette variable, un dossier comme « /storage/Video » serait pourtant
    monte mais invisible dans l'explorateur, et impossible a ajouter.

    Vide = les sources et la bibliotheque, comme avant."""

    # --- Fournisseurs (hors prefixe SORTILEGE_) ---
    tmdb_api_key: str = Field(default="", alias="TMDB_API_KEY")
    """Repli seulement : la cle se saisit dans les preferences.

    Elle survit ici pour les installations deja en place, dont le .env porte la
    cle depuis le premier jour — la retirer les aurait cassees en silence, ce
    qui est exactement ce qu'une amelioration de confort ne doit pas faire.
    Voir ``api/deps.py``, qui arbitre entre les deux."""

    # Rien ici pour le resolveur IA : fournisseur, cle, modele et taille de lot
    # vivent dans les preferences et se changent depuis l'interface, sans
    # redemarrer. Les avoir aussi dans l'environnement donnait des variables
    # que le resolveur ne lisait jamais — et l'une d'elles refusait carrement
    # le demarrage, au nom d'un reglage sans effet.

    # --- Securite ---
    secret_key: str = ""
    admin_password: str = ""

    # --- Comportement ---
    # Les defauts viennent de scoring.py, seul endroit ou ces seuils sont
    # ecrits : c'est la que la decision est prise, et deux declarations
    # finissent toujours par diverger sans que rien ne le signale.
    auto_apply_threshold: float = AUTO_APPLY_THRESHOLD
    reject_threshold: float = REJECT_THRESHOLD

    @field_validator("source_roots", "allowed_roots", mode="before")
    @classmethod
    def _split_roots(cls, v: object) -> object:
        """Accepte 'a:b:c' depuis l'environnement."""
        if isinstance(v, str):
            return [Path(p) for p in v.split(":") if p.strip()]
        return v

    @field_validator("reject_threshold")
    @classmethod
    def _thresholds_ordered(cls, v: float, info) -> float:
        auto = info.data.get("auto_apply_threshold")
        if auto is not None and v >= auto:
            raise ValueError(
                "reject_threshold doit etre strictement inferieur a "
                "auto_apply_threshold, sinon la file de revue est vide et tout "
                "bascule en automatique ou en rejet."
            )
        return v

    def check_runtime(self) -> list[str]:
        """Erreurs bloquantes au demarrage. Retourne la liste des problemes."""
        problems: list[str] = []
        if not self.secret_key or len(self.secret_key) < 32:
            problems.append("SORTILEGE_SECRET_KEY absent ou trop court (min 32 caracteres)")
        if not self.admin_password:
            problems.append("SORTILEGE_ADMIN_PASSWORD absent")
        if not self.source_roots:
            problems.append("SORTILEGE_SOURCE_ROOTS vide")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
