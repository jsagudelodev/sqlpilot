"""Configuración de SQLPilot: perfiles de conexión y proveedor LLM.

Se lee de `sqlpilot.toml` (buscado en el directorio actual, luego en `~/.sqlpilot/`)
y de variables de entorno con prefijo `SQLPILOT_`. Las variables de entorno ganan.

Ejemplo de sqlpilot.toml:

    [llm]
    proveedor = "anthropic"          # anthropic | openai | ollama
    modelo = "claude-opus-5"
    # api_key se toma de ANTHROPIC_API_KEY / OPENAI_API_KEY
    # base_url = "http://localhost:11434/v1"   # solo ollama

    [perfiles.produccion]
    servidor = "SRV-SQL01"
    base_datos = "LrpDB"
    autenticacion = "windows"        # windows | sql
    driver = "ODBC Driver 18 for SQL Server"
    confiar_certificado = true

    [perfiles.qa]
    servidor = "srv-qa,1433"
    base_datos = "LrpDB"
    autenticacion = "sql"
    usuario = "sqlpilot_ro"
    # password se toma de SQLPILOT_PERFIL_QA_PASSWORD
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ARCHIVO_CONFIG = "sqlpilot.toml"


def _cargar_env() -> None:
    """Carga `.env` desde el directorio actual, la raíz del backend o ~/.sqlpilot (sin pisar variables ya definidas)."""
    for ruta in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env", Path.home() / ".sqlpilot" / ".env"):
        if ruta.exists():
            load_dotenv(ruta, override=False)


class PerfilConexion(BaseModel):
    nombre: str
    servidor: str
    base_datos: str = "master"
    autenticacion: Literal["windows", "sql"] = "windows"
    usuario: str | None = None
    password: str | None = None
    driver: str = "ODBC Driver 18 for SQL Server"
    confiar_certificado: bool = True
    cifrar: bool = True
    timeout_conexion: int = 15
    timeout_consulta: int = 60

    def cadena_conexion(self) -> str:
        partes = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.servidor}",
            f"DATABASE={self.base_datos}",
            f"Encrypt={'yes' if self.cifrar else 'no'}",
            f"TrustServerCertificate={'yes' if self.confiar_certificado else 'no'}",
            "APP=SQLPilot",
        ]
        if self.autenticacion == "windows":
            partes.append("Trusted_Connection=yes")
        else:
            if not self.usuario:
                raise ValueError(f"El perfil '{self.nombre}' usa autenticación SQL pero no tiene usuario.")
            password = self.password or os.environ.get(f"SQLPILOT_PERFIL_{self.nombre.upper()}_PASSWORD")
            if password is None:
                raise ValueError(
                    f"Falta la contraseña del perfil '{self.nombre}'. "
                    f"Defínela en SQLPILOT_PERFIL_{self.nombre.upper()}_PASSWORD."
                )
            partes.append(f"UID={self.usuario}")
            partes.append("PWD={" + password.replace("}", "}}") + "}")  # llaves: escapa ; = } dentro de la clave
        return ";".join(partes)


class ConfigLLM(BaseModel):
    proveedor: Literal["anthropic", "openai", "gemini", "ollama"] = "anthropic"
    modelo: str = "claude-opus-5"
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 16000
    esfuerzo: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_iteraciones: int = 20


class ConfigAgente(BaseModel):
    max_filas_resultado: int = 200
    max_caracteres_resultado: int = 12000


class Configuracion(BaseModel):
    llm: ConfigLLM = Field(default_factory=ConfigLLM)
    agente: ConfigAgente = Field(default_factory=ConfigAgente)
    perfiles: dict[str, PerfilConexion] = Field(default_factory=dict)
    perfil_default: str | None = None
    ruta_archivo: Path | None = None

    def perfil(self, nombre: str | None = None) -> PerfilConexion:
        nombre = nombre or self.perfil_default or os.environ.get("SQLPILOT_PERFIL")
        if not nombre:
            if len(self.perfiles) == 1:
                return next(iter(self.perfiles.values()))
            raise ValueError(
                "No se indicó perfil. Usa --perfil, SQLPILOT_PERFIL o define perfil_default en sqlpilot.toml. "
                f"Perfiles disponibles: {', '.join(self.perfiles) or '(ninguno)'}"
            )
        if nombre not in self.perfiles:
            raise ValueError(f"El perfil '{nombre}' no existe. Disponibles: {', '.join(self.perfiles) or '(ninguno)'}")
        return self.perfiles[nombre]


def _buscar_archivo(ruta: str | Path | None) -> Path | None:
    if ruta:
        p = Path(ruta)
        return p if p.exists() else None
    candidatos = [
        Path.cwd() / ARCHIVO_CONFIG,
        Path(os.environ.get("SQLPILOT_CONFIG", "")) if os.environ.get("SQLPILOT_CONFIG") else None,
        Path.home() / ".sqlpilot" / ARCHIVO_CONFIG,
    ]
    for c in candidatos:
        if c and c.exists():
            return c
    return None


def cargar_configuracion(ruta: str | Path | None = None) -> Configuracion:
    """Carga .env, luego sqlpilot.toml y aplica overrides de entorno."""
    _cargar_env()
    datos: dict = {}
    archivo = _buscar_archivo(ruta)
    if archivo:
        with archivo.open("rb") as f:
            datos = tomllib.load(f)

    perfiles: dict[str, PerfilConexion] = {}
    for nombre, valores in (datos.get("perfiles") or {}).items():
        perfiles[nombre] = PerfilConexion(nombre=nombre, **valores)

    # Perfil implícito desde variables de entorno (útil sin archivo de config).
    if os.environ.get("SQLPILOT_SERVIDOR"):
        perfiles.setdefault(
            "env",
            PerfilConexion(
                nombre="env",
                servidor=os.environ["SQLPILOT_SERVIDOR"],
                base_datos=os.environ.get("SQLPILOT_BASE_DATOS", "master"),
                autenticacion="sql" if os.environ.get("SQLPILOT_USUARIO") else "windows",
                usuario=os.environ.get("SQLPILOT_USUARIO"),
                password=os.environ.get("SQLPILOT_PASSWORD"),
                driver=os.environ.get("SQLPILOT_DRIVER", "ODBC Driver 18 for SQL Server"),
            ),
        )

    llm_datos = dict(datos.get("llm") or {})
    for clave, env in (("proveedor", "SQLPILOT_LLM_PROVEEDOR"), ("modelo", "SQLPILOT_LLM_MODELO"),
                       ("base_url", "SQLPILOT_LLM_BASE_URL"), ("api_key", "SQLPILOT_LLM_API_KEY")):
        if os.environ.get(env):
            llm_datos[clave] = os.environ[env]

    return Configuracion(
        llm=ConfigLLM(**llm_datos),
        agente=ConfigAgente(**(datos.get("agente") or {})),
        perfiles=perfiles,
        perfil_default=datos.get("perfil_default"),
        ruta_archivo=archivo,
    )
