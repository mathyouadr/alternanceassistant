"""AlternanceAssistant - suivi de recherche d'alternance, relances et envoi de candidatures via Gmail SMTP."""
import csv
import io
import json
import mimetypes
import os
import smtplib
import sqlite3
import ssl
import unicodedata
import urllib.request
from collections import Counter
import webbrowser
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, url_for)

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data" / "alternance.db"
CONFIG_PATH = BASE / "data" / "config.json"
UPLOADS = BASE / "data" / "pieces_jointes"

STATUTS = ["À démarcher", "Contacté", "Relancé", "Entretien", "Refusé", "Accepté", "Abandonné"]
STATUTS_ACTIFS = ["Contacté", "Relancé"]  # statuts pour lesquels on attend une réponse

DEFAULT_CONFIG = {
    "nom": "",
    "gmail": "",
    "app_password": "",
    "signature": "Cordialement,\n{mon_nom}",
    "delai_relance": 7,
    "anthropic_key": "",
    "anthropic_model": "claude-sonnet-5-5",
    "poste_defaut": "",  # poste utilisé quand la fiche entreprise n'en a pas
    "profil": "",  # courte description de toi pour l'IA (formation, rythme, compétences)
}

DEFAULT_TEMPLATES = [
    ("Candidature spontanée", "candidature",
     "Candidature en alternance - {poste}",
     "Bonjour {contact},\n\nActuellement à la recherche d'une alternance en tant que {poste}, "
     "je me permets de vous adresser ma candidature au sein de {entreprise}.\n\n"
     "Votre entreprise m'intéresse particulièrement et je serais ravi(e) de mettre mes compétences "
     "au service de vos équipes tout en poursuivant ma formation.\n\n"
     "Vous trouverez ci-joint mon CV. Je reste à votre disposition pour un échange à votre convenance.\n\n"
     "{signature}"),
    ("Réponse à une offre", "candidature",
     "Candidature - Offre {poste}",
     "Bonjour {contact},\n\nJ'ai pris connaissance avec intérêt de votre offre d'alternance pour le poste de {poste} "
     "chez {entreprise}, et je souhaite vous proposer ma candidature.\n\n"
     "Mon profil et ma motivation correspondent aux missions décrites, et je serais heureux(se) d'en discuter avec vous.\n\n"
     "Vous trouverez mon CV en pièce jointe.\n\n{signature}"),
    ("Relance polie", "relance",
     "Relance - Candidature alternance {poste}",
     "Bonjour {contact},\n\nJe me permets de revenir vers vous concernant ma candidature en alternance "
     "pour le poste de {poste}, envoyée le {date_contact}.\n\n"
     "Je reste très motivé(e) à l'idée de rejoindre {entreprise} et serais ravi(e) d'échanger avec vous.\n\n"
     "{signature}"),
]

# textes libres à copier-coller (formulaires en ligne, LinkedIn, téléphone…)
DEFAULT_TEXTES = [
    ("Message LinkedIn — prise de contact",
     "Bonjour,\n\nJe suis à la recherche d'une alternance en tant que {poste} et {entreprise} m'intéresse "
     "beaucoup. Seriez-vous la bonne personne pour en parler, ou pourriez-vous m'orienter vers le bon interlocuteur ?\n\n"
     "Merci d'avance pour votre retour,\n{mon_nom}"),
    ("Message LinkedIn — relance",
     "Bonjour,\n\nJe me permets de revenir vers vous au sujet de ma candidature en alternance ({poste}) chez "
     "{entreprise}. Je reste très motivé(e) et disponible pour un échange quand cela vous arrange.\n\n"
     "Bonne journée,\n{mon_nom}"),
    ("Présentation courte (champ « Présentez-vous »)",
     "Étudiant(e) en [formation] à [école], je recherche une alternance de [durée] en tant que {poste} "
     "à partir de [date], au rythme [rythme]. Curieux(se) et rigoureux(se), j'ai déjà travaillé sur "
     "[projet / expérience], ce qui m'a permis de développer [compétences]."),
    ("« Pourquoi notre entreprise ? »",
     "{entreprise} m'attire pour [activité / produits / valeurs]. Rejoindre vos équipes en alternance me "
     "permettrait de mettre en pratique ma formation sur des projets concrets, tout en apportant [ce que tu "
     "apportes]. Je serais fier(e) de contribuer à [projet ou objectif de l'entreprise]."),
    ("Appel téléphonique — script",
     "Bonjour, je m'appelle {mon_nom}. Je suis à la recherche d'une alternance en tant que {poste} et "
     "je souhaitais savoir si {entreprise} recrutait des alternants cette année.\n"
     "→ Si oui : à qui puis-je envoyer mon CV ? Quelle adresse mail ?\n"
     "→ Si non : pensez-vous que ce sera le cas plus tard ? Puis-je quand même vous laisser mon CV ?\n"
     "Merci beaucoup, bonne journée !"),
]

MOYENS = ["Site de l'entreprise", "LinkedIn", "Indeed", "Welcome to the Jungle", "La bonne alternance",
          "France Travail", "Téléphone", "Sur place / en main propre", "Salon / forum", "Via l'école"]

app = Flask(__name__)
app.secret_key = os.urandom(24)


# ---------------------------------------------------------------- config / db
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS entreprises (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL, poste TEXT, contact TEXT, email TEXT, telephone TEXT,
        ville TEXT, lien TEXT, source TEXT, statut TEXT DEFAULT 'À démarcher',
        priorite INTEGER DEFAULT 2, notes TEXT,
        date_ajout TEXT, date_contact TEXT, prochaine_relance TEXT, nb_relances INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entreprise_id INTEGER, type TEXT, destinataire TEXT, sujet TEXT, corps TEXT,
        pieces TEXT, date_envoi TEXT, statut TEXT
    );
    CREATE TABLE IF NOT EXISTS modeles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, type TEXT, sujet TEXT, corps TEXT
    );
    CREATE TABLE IF NOT EXISTS textes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT, contenu TEXT
    );
    """)
    if conn.execute("SELECT COUNT(*) FROM modeles").fetchone()[0] == 0:
        conn.executemany("INSERT INTO modeles (nom,type,sujet,corps) VALUES (?,?,?,?)", DEFAULT_TEMPLATES)
    if conn.execute("SELECT COUNT(*) FROM textes").fetchone()[0] == 0:
        conn.executemany("INSERT INTO textes (titre,contenu) VALUES (?,?)", DEFAULT_TEXTES)
    conn.commit()
    conn.close()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)


def today():
    return date.today().isoformat()


def plus_jours(n):
    return (date.today() + timedelta(days=int(n))).isoformat()


@app.template_filter("fr")
def fr_date(s):
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return s


@app.template_filter("ecart")
def ecart(s):
    if not s:
        return ""
    d = (date.fromisoformat(s[:10]) - date.today()).days
    if d == 0:
        return "aujourd'hui"
    return f"dans {d} j" if d > 0 else f"en retard de {-d} j"


@app.context_processor
def inject():
    n = db().execute(
        f"SELECT COUNT(*) FROM entreprises WHERE statut IN ({','.join('?'*len(STATUTS_ACTIFS))}) "
        "AND prochaine_relance IS NOT NULL AND prochaine_relance <= ?", (*STATUTS_ACTIFS, today())).fetchone()[0]
    return {"nb_relances_dues": n, "STATUTS": STATUTS, "MOYENS": MOYENS, "today": today(),
            "cfg_ok": bool(load_config()["app_password"])}


# ---------------------------------------------------------------- tableau de bord
@app.route("/")
def dashboard():
    c = db()
    stats = {s: c.execute("SELECT COUNT(*) FROM entreprises WHERE statut=?", (s,)).fetchone()[0] for s in STATUTS}
    total = sum(stats.values())
    envoyes = c.execute("SELECT COUNT(*) FROM emails WHERE statut='envoyé'").fetchone()[0]
    semaine = c.execute("SELECT COUNT(*) FROM emails WHERE statut='envoyé' AND date_envoi >= ?",
                        (plus_jours(-7),)).fetchone()[0]
    dues = c.execute(
        "SELECT * FROM entreprises WHERE statut IN ('Contacté','Relancé') AND prochaine_relance <= ? "
        "ORDER BY prochaine_relance LIMIT 8", (today(),)).fetchall()
    a_demarcher = c.execute(
        "SELECT * FROM entreprises WHERE statut='À démarcher' ORDER BY priorite, date_ajout LIMIT 8").fetchall()
    derniers = c.execute(
        "SELECT e.*, n.nom AS entreprise FROM emails e LEFT JOIN entreprises n ON n.id=e.entreprise_id "
        "ORDER BY e.date_envoi DESC LIMIT 8").fetchall()
    # taux de réponse : uniquement sur les entreprises réellement contactées (ni "À démarcher",
    # ni "Abandonné" avant tout contact)
    reponses = stats["Entretien"] + stats["Refusé"] + stats["Accepté"]
    contactes = reponses + stats["Contacté"] + stats["Relancé"] + c.execute(
        "SELECT COUNT(*) FROM entreprises WHERE statut='Abandonné' AND date_contact IS NOT NULL").fetchone()[0]
    taux = {"pct": round(100 * reponses / contactes) if contactes else None, "rep": reponses, "cont": contactes}
    return render_template("dashboard.html", stats=stats, total=total, envoyes=envoyes, semaine=semaine,
                           dues=dues, a_demarcher=a_demarcher, derniers=derniers, taux=taux)


# ---------------------------------------------------------------- index entreprises
@app.route("/entreprises")
def entreprises():
    q = request.args.get("q", "").strip()
    statut = request.args.get("statut", "")
    ville = request.args.get("ville", "")
    sql, args = "SELECT * FROM entreprises WHERE 1=1", []
    if q:
        sql += " AND (nom LIKE ? OR poste LIKE ? OR ville LIKE ? OR contact LIKE ? OR notes LIKE ?)"
        args += [f"%{q}%"] * 5
    if statut:
        sql += " AND statut=?"
        args.append(statut)
    if ville:
        # une entreprise peut être sur plusieurs villes : "Saint-Etienne / Lyon"
        sql += " AND (' / ' || ville || ' / ') LIKE ?"
        args.append(f"% / {ville} / %")
    sql += " ORDER BY CASE statut WHEN 'À démarcher' THEN 1 WHEN 'Contacté' THEN 2 WHEN 'Relancé' THEN 3 " \
           "WHEN 'Entretien' THEN 0 ELSE 9 END, priorite, nom"
    rows = db().execute(sql, args).fetchall()
    return render_template("entreprises.html", rows=rows, q=q, statut=statut, ville=ville, villes=liste_villes())


def sans_accent(s):
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


def liste_villes():
    """Villes distinctes (les valeurs multiples 'A / B' sont éclatées) avec leur nombre d'entreprises."""
    compte = Counter()
    for (v,) in db().execute("SELECT ville FROM entreprises WHERE ville IS NOT NULL AND ville != ''"):
        compte.update({x.strip() for x in v.split(" / ") if x.strip()})
    return sorted(compte.items(), key=lambda kv: sans_accent(kv[0]))


FIELDS = ["nom", "poste", "contact", "email", "telephone", "ville", "lien", "source", "statut",
          "priorite", "notes", "date_contact", "prochaine_relance"]


@app.route("/entreprise/nouvelle", methods=["GET", "POST"])
@app.route("/entreprise/<int:eid>", methods=["GET", "POST"])
def entreprise(eid=None):
    c = db()
    if request.method == "POST":
        data = {f: (request.form.get(f) or "").strip() or None for f in FIELDS}
        if not data["nom"]:
            flash("Le nom de l'entreprise est obligatoire.", "err")
            return redirect(request.url)
        data["statut"] = data["statut"] or "À démarcher"
        if eid:
            c.execute(f"UPDATE entreprises SET {','.join(f + '=?' for f in FIELDS)} WHERE id=?",
                      (*data.values(), eid))
            flash("Fiche mise à jour.", "ok")
        else:
            cur = c.execute(f"INSERT INTO entreprises ({','.join(FIELDS)}, date_ajout) VALUES "
                            f"({','.join('?' * len(FIELDS))}, ?)", (*data.values(), today()))
            eid = cur.lastrowid
            flash("Entreprise ajoutée.", "ok")
        c.commit()
        return redirect(url_for("entreprise", eid=eid))
    row = c.execute("SELECT * FROM entreprises WHERE id=?", (eid,)).fetchone() if eid else None
    emails = c.execute("SELECT * FROM emails WHERE entreprise_id=? ORDER BY date_envoi DESC",
                       (eid,)).fetchall() if eid else []
    return render_template("entreprise.html", e=row, emails=emails)


def supprimer_entreprises(ids):
    c = db()
    marks = ",".join("?" * len(ids))
    c.execute(f"DELETE FROM emails WHERE entreprise_id IN ({marks})", ids)
    n = c.execute(f"DELETE FROM entreprises WHERE id IN ({marks})", ids).rowcount
    c.commit()
    return n


def retour_liste():
    """Revient à la liste en gardant les filtres, sauf si on vient de la fiche supprimée."""
    ref = request.referrer or ""
    return redirect(ref if "/entreprises" in ref or "/relances" in ref else url_for("entreprises"))


@app.post("/entreprise/<int:eid>/supprimer")
def supprimer(eid):
    supprimer_entreprises([eid])
    flash("Entreprise supprimée.", "ok")
    return retour_liste()


@app.post("/entreprises/supprimer")
def supprimer_selection():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if ids:
        flash(f"{supprimer_entreprises(ids)} entreprise(s) supprimée(s).", "ok")
    return retour_liste()


@app.post("/email/<int:mid>/supprimer")
def supprimer_email(mid):
    db().execute("DELETE FROM emails WHERE id=?", (mid,))
    db().commit()
    flash("Mail supprimé de l'historique.", "ok")
    return redirect(request.referrer or url_for("historique"))


@app.post("/candidature-autre")
def candidature_autre():
    """Enregistre une candidature faite hors mail (site, LinkedIn, téléphone…) : statut, date et relance."""
    c, f = db(), request.form
    ids = [int(i) for i in f.getlist("ids") if i.isdigit()]
    moyen = f.get("moyen", "").strip() or "autre moyen"
    note = f.get("note", "").strip()
    try:
        jour = date.fromisoformat(f.get("date") or today())
    except ValueError:
        jour = date.today()
    prochaine = (jour + timedelta(days=int(load_config()["delai_relance"]))).isoformat()
    for eid in ids:
        # les CASE lisent l'ancienne valeur de statut
        c.execute("UPDATE entreprises SET "
                  "date_contact = CASE WHEN statut IN ('À démarcher','Abandonné') OR date_contact IS NULL "
                  "THEN ? ELSE date_contact END, "
                  "statut = CASE WHEN statut IN ('À démarcher','Abandonné') THEN 'Contacté' ELSE statut END, "
                  "prochaine_relance = ? WHERE id=?", (jour.isoformat(), prochaine, eid))
        c.execute("INSERT INTO emails (entreprise_id,type,destinataire,sujet,corps,pieces,date_envoi,statut) "
                  "VALUES (?,?,?,?,?,?,?,?)", (eid, "candidature", None, f"Candidature via {moyen}", note, None,
                                               jour.isoformat(), "hors mail"))
    c.commit()
    if ids:
        flash(f"{len(ids)} candidature(s) enregistrée(s) ({moyen}) — relance programmée le {fr_date(prochaine)}.", "ok")
    return redirect(request.referrer or url_for("entreprises"))


@app.post("/entreprise/<int:eid>/statut")
def changer_statut(eid):
    db().execute("UPDATE entreprises SET statut=? WHERE id=?", (request.form["statut"], eid))
    db().commit()
    return redirect(request.referrer or url_for("entreprises"))


@app.route("/export.csv")
def export_csv():
    rows = db().execute("SELECT * FROM entreprises ORDER BY nom").fetchall()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    if rows:
        w.writerow(rows[0].keys())
        w.writerows([tuple(r) for r in rows])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=alternance.csv"})


@app.post("/import")
def import_csv():
    f = request.files.get("fichier")
    if not f:
        return redirect(url_for("entreprises"))
    text = f.read().decode("utf-8-sig", errors="replace")
    delim = ";" if text.count(";") > text.count(",") else ","
    n = 0
    for r in csv.DictReader(io.StringIO(text), delimiter=delim):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        if not r.get("nom"):
            continue
        db().execute("INSERT INTO entreprises (nom,poste,contact,email,telephone,ville,lien,source,statut,notes,date_ajout)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (r["nom"], r.get("poste"), r.get("contact"), r.get("email"), r.get("telephone"), r.get("ville"),
                      r.get("lien"), r.get("source"), r.get("statut") or "À démarcher", r.get("notes"), today()))
        n += 1
    db().commit()
    flash(f"{n} entreprise(s) importée(s).", "ok")
    return redirect(url_for("entreprises"))


# ---------------------------------------------------------------- relances
@app.route("/relances")
def relances():
    c = db()
    base = "SELECT * FROM entreprises WHERE statut IN ('Contacté','Relancé') AND prochaine_relance IS NOT NULL "
    a_faire = c.execute(base + "AND prochaine_relance <= ? ORDER BY prochaine_relance", (today(),)).fetchall()
    a_venir = c.execute(base + "AND prochaine_relance > ? ORDER BY prochaine_relance", (today(),)).fetchall()
    sans_date = c.execute("SELECT * FROM entreprises WHERE statut IN ('Contacté','Relancé') "
                          "AND prochaine_relance IS NULL ORDER BY nom").fetchall()
    return render_template("relances.html", a_faire=a_faire, a_venir=a_venir, sans_date=sans_date)


@app.post("/relance/<int:eid>/<action>")
def relance_action(eid, action):
    c = db()
    delai = load_config()["delai_relance"]
    if action == "faite":
        c.execute("UPDATE entreprises SET statut='Relancé', nb_relances=nb_relances+1, prochaine_relance=? WHERE id=?",
                  (plus_jours(delai), eid))
        flash("Relance notée, prochaine programmée.", "ok")
    elif action == "reporter":
        c.execute("UPDATE entreprises SET prochaine_relance=? WHERE id=?", (plus_jours(request.form.get("jours", 3)), eid))
        flash("Relance reportée.", "ok")
    elif action == "stop":
        c.execute("UPDATE entreprises SET prochaine_relance=NULL WHERE id=?", (eid,))
        flash("Plus de relance pour cette entreprise.", "ok")
    c.commit()
    return redirect(request.referrer or url_for("relances"))


# ---------------------------------------------------------------- assistant de candidature
def remplir(texte, e, cfg):
    sig = (cfg.get("signature") or "").replace("{mon_nom}", cfg.get("nom") or "")
    vals = {
        "entreprise": (e["nom"] if e else "") or "",
        "poste": (e["poste"] if e else "") or cfg.get("poste_defaut") or "alternant(e)",
        "contact": (e["contact"] if e else "") or "Madame, Monsieur",
        "ville": (e["ville"] if e else "") or "",
        "date_contact": fr_date(e["date_contact"]) if e and e["date_contact"] else "précédemment",
        "mon_nom": cfg.get("nom") or "",
        "signature": sig,
    }
    for k, v in vals.items():
        texte = texte.replace("{" + k + "}", v)
    return texte


@app.route("/assistant")
def assistant():
    c = db()
    eid = request.args.get("e", type=int)
    type_ = request.args.get("type", "candidature")
    e = c.execute("SELECT * FROM entreprises WHERE id=?", (eid,)).fetchone() if eid else None
    if e and not request.args.get("type"):
        type_ = "relance" if e["statut"] in STATUTS_ACTIFS else "candidature"
    modeles = c.execute("SELECT * FROM modeles ORDER BY type, nom").fetchall()
    mid = request.args.get("m", type=int)
    m = next((x for x in modeles if x["id"] == mid), None) or next((x for x in modeles if x["type"] == type_), None)
    cfg = load_config()
    sujet = remplir(m["sujet"], e, cfg) if m else ""
    corps = remplir(m["corps"], e, cfg) if m else ""
    ents = c.execute("SELECT id, nom, poste, statut, email FROM entreprises ORDER BY nom").fetchall()
    pjs = sorted(p.name for p in UPLOADS.iterdir() if p.is_file())
    return render_template("assistant.html", e=e, ents=ents, modeles=modeles, m=m, sujet=sujet, corps=corps,
                           type_=type_, pjs=pjs, ia=bool(cfg["anthropic_key"]))


@app.post("/api/reecrire")
def api_reecrire():
    cfg = load_config()
    if not cfg["anthropic_key"]:
        return jsonify(error="Ajoute une clé API Anthropic dans Paramètres pour utiliser la réécriture IA."), 400
    d = request.get_json()
    e = db().execute("SELECT * FROM entreprises WHERE id=?", (d.get("eid"),)).fetchone() if d.get("eid") else None
    contexte = ""
    if e:
        contexte = (f"Entreprise : {e['nom']}\nPoste visé : {e['poste'] or ''}\nContact : {e['contact'] or ''}\n"
                    f"Ville : {e['ville'] or ''}\nNotes sur l'entreprise : {e['notes'] or ''}\n")
    prompt = (
        "Tu aides un(e) étudiant(e) à rédiger des emails de recherche d'alternance en français.\n"
        f"Profil du candidat : {cfg.get('profil') or 'non renseigné'}\n{contexte}\n"
        f"Consigne : {d.get('consigne') or 'Rends le mail plus percutant, naturel et professionnel, sans en faire trop.'}\n\n"
        "Réécris le mail ci-dessous. N'invente aucune expérience ni fait. Garde la signature.\n"
        "Réponds UNIQUEMENT en JSON : {\"sujet\": \"...\", \"corps\": \"...\"}\n\n"
        f"SUJET : {d.get('sujet', '')}\n\nCORPS :\n{d.get('corps', '')}"
    )
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": cfg["anthropic_model"], "max_tokens": 2000,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": cfg["anthropic_key"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = "".join(b.get("text", "") for b in json.load(r)["content"])
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
        return jsonify(json.loads(txt))
    except urllib.error.HTTPError as ex:
        return jsonify(error=f"Erreur API ({ex.code}) : {ex.read().decode()[:300]}"), 502
    except Exception as ex:  # noqa: BLE001
        return jsonify(error=f"Échec de la réécriture : {ex}"), 502


def envoyer_mail(cfg, dest, sujet, corps, pieces):
    msg = EmailMessage()
    msg["From"] = f"{cfg['nom']} <{cfg['gmail']}>" if cfg["nom"] else cfg["gmail"]
    msg["To"] = dest
    msg["Subject"] = sujet
    msg.set_content(corps)
    for nom in pieces:
        p = UPLOADS / nom
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg["gmail"], cfg["app_password"].replace(" ", ""))
        s.send_message(msg)


@app.post("/assistant/envoyer")
def assistant_envoyer():
    c, cfg = db(), load_config()
    f = request.form
    eid = f.get("eid", type=int)
    dest, sujet, corps, type_ = f["dest"].strip(), f["sujet"], f["corps"], f.get("type", "candidature")
    pieces = [p for p in f.getlist("pieces") if (UPLOADS / p).is_file()]
    brouillon = f.get("action") == "brouillon"
    if not brouillon:
        if not (cfg["gmail"] and cfg["app_password"]):
            flash("Configure ton adresse Gmail et ton mot de passe d'application dans Paramètres.", "err")
            return redirect(request.referrer)
        if not dest:
            flash("Destinataire manquant.", "err")
            return redirect(request.referrer)
        try:
            envoyer_mail(cfg, dest, sujet, corps, pieces)
        except smtplib.SMTPAuthenticationError:
            flash("Authentification Gmail refusée : vérifie ton mot de passe d'application.", "err")
            return redirect(request.referrer)
        except Exception as ex:  # noqa: BLE001
            flash(f"Échec de l'envoi : {ex}", "err")
            return redirect(request.referrer)
    c.execute("INSERT INTO emails (entreprise_id,type,destinataire,sujet,corps,pieces,date_envoi,statut) "
              "VALUES (?,?,?,?,?,?,?,?)", (eid, type_, dest, sujet, corps, ", ".join(pieces),
                                           datetime.now().isoformat(timespec="minutes"),
                                           "brouillon" if brouillon else "envoyé"))
    if eid and not brouillon:
        prochaine = plus_jours(cfg["delai_relance"])
        if type_ == "relance":
            c.execute("UPDATE entreprises SET statut='Relancé', nb_relances=nb_relances+1, prochaine_relance=? "
                      "WHERE id=?", (prochaine, eid))
        else:
            c.execute("UPDATE entreprises SET statut=CASE WHEN statut='À démarcher' THEN 'Contacté' ELSE statut END, "
                      "date_contact=?, prochaine_relance=?, email=COALESCE(NULLIF(email,''), ?) WHERE id=?",
                      (today(), prochaine, dest, eid))
    c.commit()
    flash("Brouillon enregistré." if brouillon else f"Mail envoyé à {dest} ✔ — relance programmée.", "ok")
    return redirect(url_for("entreprise", eid=eid) if eid else url_for("historique"))


@app.route("/historique")
def historique():
    rows = db().execute("SELECT e.*, n.nom AS entreprise FROM emails e LEFT JOIN entreprises n "
                        "ON n.id=e.entreprise_id ORDER BY e.date_envoi DESC").fetchall()
    return render_template("historique.html", rows=rows)


# ---------------------------------------------------------------- modèles
@app.route("/modeles", methods=["GET", "POST"])
def modeles():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("id"):
            c.execute("UPDATE modeles SET nom=?, type=?, sujet=?, corps=? WHERE id=?",
                      (f["nom"], f["type"], f["sujet"], f["corps"], f["id"]))
        else:
            c.execute("INSERT INTO modeles (nom,type,sujet,corps) VALUES (?,?,?,?)",
                      (f["nom"], f["type"], f["sujet"], f["corps"]))
        c.commit()
        flash("Modèle enregistré.", "ok")
        return redirect(url_for("modeles"))
    return render_template("modeles.html", rows=c.execute("SELECT * FROM modeles ORDER BY type, nom").fetchall())


@app.post("/modeles/<int:mid>/supprimer")
def modele_supprimer(mid):
    db().execute("DELETE FROM modeles WHERE id=?", (mid,))
    db().commit()
    return redirect(url_for("modeles"))


# ---------------------------------------------------------------- textes à copier-coller
@app.route("/textes", methods=["GET", "POST"])
def textes():
    c = db()
    if request.method == "POST":
        f = request.form
        if f.get("id"):
            c.execute("UPDATE textes SET titre=?, contenu=? WHERE id=?", (f["titre"], f["contenu"], f["id"]))
        else:
            c.execute("INSERT INTO textes (titre, contenu) VALUES (?,?)", (f["titre"], f["contenu"]))
        c.commit()
        flash("Texte enregistré.", "ok")
        return redirect(url_for("textes", e=f.get("e") or None))
    cfg = load_config()
    eid = request.args.get("e", type=int)
    e = c.execute("SELECT * FROM entreprises WHERE id=?", (eid,)).fetchone() if eid else None

    def rendu(t):
        if e:
            return remplir(t, e, cfg)
        # sans entreprise choisie : on ne remplit que tes infos, les {variables} restent visibles
        sig = (cfg.get("signature") or "").replace("{mon_nom}", cfg.get("nom") or "")
        t = t.replace("{signature}", sig).replace("{mon_nom}", cfg.get("nom") or "{mon_nom}")
        return t.replace("{poste}", cfg["poste_defaut"]) if cfg.get("poste_defaut") else t

    rows = [dict(r, rendu=rendu(r["contenu"])) for r in c.execute("SELECT * FROM textes ORDER BY id")]
    mails = [dict(r, sujet_rendu=rendu(r["sujet"]), corps_rendu=rendu(r["corps"]))
             for r in c.execute("SELECT * FROM modeles ORDER BY type, nom")]
    ents = c.execute("SELECT id, nom, poste FROM entreprises ORDER BY nom").fetchall()
    return render_template("textes.html", rows=rows, mails=mails, ents=ents, e=e)


@app.post("/textes/<int:tid>/supprimer")
def texte_supprimer(tid):
    db().execute("DELETE FROM textes WHERE id=?", (tid,))
    db().commit()
    flash("Texte supprimé.", "ok")
    return redirect(request.referrer or url_for("textes"))


# ---------------------------------------------------------------- paramètres
@app.route("/parametres", methods=["GET", "POST"])
def parametres():
    cfg = load_config()
    if request.method == "POST":
        f = request.form
        for k in ["nom", "gmail", "signature", "anthropic_model", "profil", "poste_defaut"]:
            cfg[k] = f.get(k, "").strip()
        cfg["delai_relance"] = max(1, f.get("delai_relance", type=int) or 7)
        # champs secrets : on ne remplace que si l'utilisateur a saisi quelque chose
        if f.get("app_password"):
            cfg["app_password"] = f["app_password"].strip()
        if f.get("anthropic_key"):
            cfg["anthropic_key"] = f["anthropic_key"].strip()
        for up in request.files.getlist("pj"):
            if up and up.filename:
                up.save(UPLOADS / Path(up.filename).name)
        save_config(cfg)
        flash("Paramètres enregistrés.", "ok")
        return redirect(url_for("parametres"))
    pjs = sorted(p.name for p in UPLOADS.iterdir() if p.is_file())
    return render_template("parametres.html", cfg=cfg, pjs=pjs)


@app.post("/parametres/pj/<path:nom>/supprimer")
def pj_supprimer(nom):
    p = UPLOADS / Path(nom).name
    if p.is_file():
        p.unlink()
    return redirect(url_for("parametres"))


@app.post("/parametres/test")
def test_smtp():
    cfg = load_config()
    try:
        envoyer_mail(cfg, cfg["gmail"], "Test AlternanceAssistant", "La connexion SMTP Gmail fonctionne ✔", [])
        flash(f"Mail de test envoyé à {cfg['gmail']}.", "ok")
    except Exception as ex:  # noqa: BLE001
        flash(f"Échec du test SMTP : {ex}", "err")
    return redirect(url_for("parametres"))


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5055))
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(f"http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
