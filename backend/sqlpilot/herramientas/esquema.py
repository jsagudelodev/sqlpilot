"""Esquema: tablas, columnas, procedimientos (definición completa) y dependencias."""

from __future__ import annotations

from sqlpilot.db.conexion import ConexionSql
from sqlpilot.herramientas.base import herramienta


@herramienta(
    "buscar_objetos",
    "Busca tablas, vistas, procedimientos y funciones por nombre (LIKE) en la base de datos actual.",
    "esquema",
    filtro="Texto a buscar en el nombre del objeto.",
    tipo="Filtrar por tipo: tabla | vista | procedimiento | funcion (opcional).",
)
def buscar_objetos(conexion: ConexionSql, filtro: str, tipo: str | None = None) -> dict:
    tipos = {"tabla": "('U')", "vista": "('V')", "procedimiento": "('P')", "funcion": "('FN','IF','TF')"}
    filtro_tipo = f"AND o.type IN {tipos[tipo]}" if tipo in tipos else "AND o.type IN ('U','V','P','FN','IF','TF')"
    filas = conexion.consultar_dicts(
        f"""
        SELECT TOP 100 s.name AS esquema, o.name AS nombre, o.type_desc AS tipo, o.modify_date AS modificado
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
        WHERE o.name LIKE '%' + ? + '%' {filtro_tipo}
        ORDER BY o.type_desc, o.name
        """,
        (filtro,),
    )
    return {"total": len(filas), "objetos": filas}


@herramienta(
    "describir_tabla",
    "Columnas (tipo, nulabilidad, default, identidad, PK), claves foráneas entrantes/salientes, filas y tamaño de una tabla.",
    "esquema",
    tabla="Nombre con o sin esquema (ej. dbo.Despachos).",
)
def describir_tabla(conexion: ConexionSql, tabla: str) -> dict:
    columnas = conexion.consultar_dicts(
        """
        SELECT c.column_id AS orden, c.name AS columna, t.name AS tipo,
               CASE WHEN t.name IN ('nvarchar','nchar') THEN c.max_length / 2 ELSE c.max_length END AS longitud,
               c.precision, c.scale, c.is_nullable AS nulable, c.is_identity AS identidad, c.is_computed AS calculada,
               d.definition AS valor_default,
               CASE WHEN EXISTS (SELECT 1 FROM sys.index_columns ic JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                                 WHERE i.is_primary_key = 1 AND ic.object_id = c.object_id AND ic.column_id = c.column_id) THEN 1 ELSE 0 END AS es_pk
        FROM sys.columns c
        JOIN sys.types t ON t.user_type_id = c.user_type_id
        LEFT JOIN sys.default_constraints d ON d.object_id = c.default_object_id
        WHERE c.object_id = OBJECT_ID(?)
        ORDER BY c.column_id
        """,
        (tabla,),
        max_filas=None,
    )
    if not columnas:
        return {"error": f"No se encontró la tabla '{tabla}'."}
    fks = conexion.consultar_dicts(
        """
        SELECT fk.name AS clave, OBJECT_SCHEMA_NAME(fk.parent_object_id) + '.' + OBJECT_NAME(fk.parent_object_id) AS tabla_origen,
               COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS columna_origen,
               OBJECT_SCHEMA_NAME(fk.referenced_object_id) + '.' + OBJECT_NAME(fk.referenced_object_id) AS tabla_destino,
               COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS columna_destino,
               fk.is_disabled, fk.is_not_trusted
        FROM sys.foreign_keys fk JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        WHERE fk.parent_object_id = OBJECT_ID(?) OR fk.referenced_object_id = OBJECT_ID(?)
        """,
        (tabla, tabla),
        max_filas=None,
    )
    tamano = conexion.consultar_dicts(
        """
        SELECT SUM(CASE WHEN index_id IN (0,1) THEN row_count END) AS filas, SUM(reserved_page_count) * 8 / 1024 AS total_mb
        FROM sys.dm_db_partition_stats WHERE object_id = OBJECT_ID(?)
        """,
        (tabla,),
    )
    return {"tabla": tabla, "columnas": columnas, "claves_foraneas": fks, "tamano": tamano[0] if tamano else None}


@herramienta(
    "definicion_objeto",
    """Definición T-SQL COMPLETA (sin truncar) de un procedimiento, función, vista o trigger, con sus parámetros,
    fechas y objetos de los que depende. Úsala antes de analizar el rendimiento de un SP.""",
    "esquema",
    nombre="Nombre con o sin esquema (ej. dbo.spLRP_Despachos_Listar).",
)
def definicion_objeto(conexion: ConexionSql, nombre: str) -> dict:
    cabecera = conexion.consultar_dicts(
        """
        SELECT s.name AS esquema, o.name AS nombre, o.type_desc AS tipo, o.create_date, o.modify_date,
               m.definition, m.uses_ansi_nulls, m.uses_quoted_identifier, m.is_recompiled, LEN(m.definition) AS longitud
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.sql_modules m ON m.object_id = o.object_id
        WHERE o.object_id = OBJECT_ID(?)
        """,
        (nombre,),
    )
    if not cabecera:
        return {"error": f"No se encontró el objeto '{nombre}' o no tiene definición SQL."}
    parametros = conexion.consultar_dicts(
        """
        SELECT p.parameter_id AS orden, p.name AS nombre, t.name AS tipo,
               CASE WHEN t.name IN ('nvarchar','nchar') THEN p.max_length / 2 ELSE p.max_length END AS longitud,
               p.precision, p.scale, p.is_output AS salida, p.has_default_value AS tiene_default
        FROM sys.parameters p JOIN sys.types t ON t.user_type_id = p.user_type_id
        WHERE p.object_id = OBJECT_ID(?) ORDER BY p.parameter_id
        """,
        (nombre,),
        max_filas=None,
    )
    dependencias = conexion.consultar_dicts(
        """
        SELECT DISTINCT ISNULL(referenced_schema_name, 'dbo') + '.' + referenced_entity_name AS objeto,
               referenced_minor_name AS columna, is_updated AS escribe
        FROM sys.dm_sql_referenced_entities(?, 'OBJECT')
        WHERE referenced_minor_id = 0 OR is_updated = 1
        """,
        (nombre if "." in nombre else f"dbo.{nombre}",),
        max_filas=None,
    )
    c = cabecera[0]
    return {
        "esquema": c["esquema"], "nombre": c["nombre"], "tipo": c["tipo"],
        "creado": c["create_date"], "modificado": c["modify_date"], "longitud": c["longitud"],
        "parametros": parametros, "dependencias": dependencias, "definicion": c["definition"],
    }


@herramienta(
    "quien_usa_tabla",
    "Objetos (SPs, vistas, funciones, triggers) que referencian una tabla o columna.",
    "esquema",
    tabla="Nombre de la tabla con o sin esquema.",
)
def quien_usa_tabla(conexion: ConexionSql, tabla: str) -> dict:
    filas = conexion.consultar_dicts(
        """
        SELECT DISTINCT referencing_schema_name + '.' + referencing_entity_name AS objeto, o.type_desc AS tipo
        FROM sys.dm_sql_referencing_entities(?, 'OBJECT') r
        JOIN sys.objects o ON o.object_id = r.referencing_id
        ORDER BY 1
        """,
        (tabla if "." in tabla else f"dbo.{tabla}",),
        max_filas=None,
    )
    return {"tabla": tabla, "referenciada_por": filas}
