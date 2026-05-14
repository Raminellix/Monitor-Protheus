import io
import csv
import pyodbc
from flask import Blueprint, render_template, request, jsonify, Response
from flask_login import login_required
# Importa o get_cfg do seu modulo existente de configuracoes
from .settings_ui import get_cfg 

sql_explorer_bp = Blueprint("sql_explorer", __name__)

@sql_explorer_bp.route("/sql-explorer", methods=["GET", "POST"])
@login_required
def sql_home():
    if request.method == "POST":
        query = request.form.get("sql_code", "").strip()
        force_csv = request.form.get("force_csv") == "true"

        if not query:
            return jsonify({"ok": False, "message": "Por favor, digite uma query."})

        # Trava de segurança rigorosa
        if not query.lower().startswith("select"):
            return jsonify({"ok": False, "message": "BLOQUEADO: Por segurança, apenas consultas SELECT são permitidas."})

        cfg = get_cfg()
        if not cfg.sql_host or not cfg.sql_user:
            return jsonify({"ok": False, "message": "O Banco de Dados não está configurado. Acesse a aba Configurações primeiro."})

        conn = None
        try:
            conn_str = (
                f"DRIVER={{SQL Server}};"
                f"SERVER={cfg.sql_host};"
                f"DATABASE={cfg.sql_database};"
                f"UID={cfg.sql_user};"
                f"PWD={cfg.sql_password}"
            )
            conn = pyodbc.connect(conn_str, timeout=30)
            cursor = conn.cursor()
            cursor.execute(query)
            
            # Pega o nome das colunas
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            
            # Se tiver mais de 50 registros OU se o usuario clicou em "Baixar CSV"
            if force_csv or len(rows) > 50:
                output = io.StringIO()
                # Separador de Ponto-e-virgula para Excel PT-BR
                writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL, lineterminator='\n')
                writer.writerow(columns)
                for row in rows:
                    # Converte cada item para string (evita erros com Datas ou Nulos)
                    writer.writerow([str(item) if item is not None else "" for item in row])
                
                output.seek(0)
                return Response(
                    output.getvalue(),
                    mimetype="text/csv",
                    headers={"Content-disposition": "attachment; filename=resultado_query.csv"}
                )

            # Se for menor ou igual a 50, retorna JSON para montar a tabela na tela
            data = []
            for row in rows:
                data.append([str(item) if item is not None else "" for item in row])

            return jsonify({"ok": True, "columns": columns, "data": data, "count": len(rows)})

        except Exception as e:
            return jsonify({"ok": False, "message": f"Erro de sintaxe ou banco: {str(e)}"})
        finally:
            if conn: 
                conn.close()

    return render_template("pages/sql_explorer.html")