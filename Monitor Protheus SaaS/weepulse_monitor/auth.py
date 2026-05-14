from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required
from .extensions import db
from .models import User

auth_bp = Blueprint("auth", __name__)

def has_any_user():
    return db.session.query(User.id).first() is not None

@auth_bp.route("/", methods=["GET"])
def root():
    # Se não existe usuário, manda para primeiro acesso
    if not has_any_user():
        return redirect(url_for("auth.first_access"))
    return redirect(url_for("auth.login"))

@auth_bp.route("/first-access", methods=["GET", "POST"])
def first_access():
    if has_any_user():
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not username or not password:
            flash("Informe usuário e senha.", "danger")
            return render_template("auth/first_access.html")

        if password != password2:
            flash("As senhas não conferem.", "danger")
            return render_template("auth/first_access.html")

        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Usuário criado com sucesso. Faça login.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/first_access.html")

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if not has_any_user():
        return redirect(url_for("auth.first_access"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if not user or not user.check_password(password):
            flash("Usuário ou senha inválidos.", "danger")
            return render_template("auth/login.html")

        login_user(user)
        return redirect(url_for("services.services_home"))

    return render_template("auth/login.html")

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))