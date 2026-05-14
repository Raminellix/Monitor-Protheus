from flask import Blueprint, render_template, jsonify
from flask_login import login_required
from .models import AppConfig
from .background_worker import noc_cache 

noc_bp = Blueprint("noc", __name__)

@noc_bp.route("/")
@login_required
def noc_dashboard():
    cfg = AppConfig.query.first()
    return render_template("pages/noc.html", cfg=cfg)

# NOVA ROTA: Mapa Visual da Infraestrutura
@noc_bp.route("/topology")
@login_required
def noc_topology():
    return render_template("pages/topology.html")

@noc_bp.route("/data")
@login_required
def get_noc_data():
    return jsonify({
        "ok": True, 
        "servers": noc_cache.get("servers", []), 
        "services": noc_cache.get("services", []),
        "last_update": noc_cache.get("last_update", "A aguardar...")
    })