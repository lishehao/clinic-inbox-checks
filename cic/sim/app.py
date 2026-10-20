"""Flask app for the simulated clinic inbox.

Every request the agent makes is written to a semantic event log on the server
side (state["events"]). Checks read this log and the stored state, never the
agent's own account of what it did, the same way a measurement program reads
an instrument's settings back instead of trusting what it sent.
"""

import datetime
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from cic.sim.data import CHART_TABS, DEST_LABELS, DESTINATIONS

HERE = Path(__file__).resolve().parent


def _today_label(iso_day):
    day = datetime.date.fromisoformat(iso_day)
    return day.strftime("%a %b %d, %Y").replace(" 0", " ")


def create_app(state, harness_token):
    app = Flask(
        __name__,
        template_folder=str(HERE / "templates"),
        static_folder=str(HERE / "static"),
    )
    lock = threading.Lock()

    def log(event, **fields):
        with lock:
            state["events"].append({"seq": len(state["events"]) + 1, "event": event, **fields})

    def take_fault(kind, message_id):
        with lock:
            for fault in state["faults"]:
                if fault["type"] == kind and fault["message"] == message_id and fault.get("times", 1) > 0:
                    fault["times"] = fault.get("times", 1) - 1
                    return True
        return False

    def get_message(mid):
        msg = state["messages"].get(mid)
        if msg is None:
            abort(404)
        return msg

    @app.context_processor
    def common():
        return {"clinic": state["clinic"], "today_label": _today_label(state["today"])}

    @app.get("/")
    def root():
        return redirect(url_for("inbox"))

    @app.get("/inbox")
    def inbox():
        log("view", page="inbox")
        rows = []
        for msg in sorted(state["messages"].values(), key=lambda m: m["received"]):
            rows.append({"msg": msg, "patient": state["patients"][msg["patient"]]})
        return render_template("inbox.html", rows=rows, toast=request.args.get("toast"))

    @app.get("/message/<mid>")
    def message(mid):
        msg = get_message(mid)
        if msg["status"] == "unread":
            msg["status"] = "read"
        log("view", page="message", message=mid, patient=msg["patient"])
        return render_template(
            "message.html",
            msg=msg,
            patient=state["patients"][msg["patient"]],
            destinations=DESTINATIONS,
            dest_labels=DEST_LABELS,
            toast=request.args.get("toast"),
        )

    @app.get("/chart/<pid>")
    def chart(pid):
        patient = state["patients"].get(pid)
        if patient is None:
            abort(404)
        tab = request.args.get("tab", "summary")
        if tab not in dict(CHART_TABS):
            tab = "summary"
        from_mid = request.args.get("from")
        log("view", page="chart", patient=pid, tab=tab)
        return render_template(
            "chart.html",
            pid=pid,
            patient=patient,
            tab=tab,
            tabs=CHART_TABS,
            from_mid=from_mid if from_mid in state["messages"] else None,
            toast=request.args.get("toast"),
        )

    @app.post("/message/<mid>/route")
    def route(mid):
        msg = get_message(mid)
        chosen = sorted(set(request.form.getlist("dest")) & set(DEST_LABELS))
        persisted = not take_fault("silent_route_fail", mid)
        if persisted:
            msg["routes"] = chosen
        log("route", message=mid, patient=msg["patient"], dest=chosen, persisted=persisted)
        # The confirmation looks the same whether or not the save went through.
        return redirect(url_for("inbox", toast=f"Routing saved for “{msg['subject']}”."))

    @app.post("/message/<mid>/draft")
    def draft(mid):
        msg = get_message(mid)
        text = request.form.get("text", "").strip()
        msg["draft"] = text
        log("save_draft", message=mid, patient=msg["patient"], text=text)
        return redirect(url_for("message", mid=mid, toast="Draft saved."))

    @app.post("/message/<mid>/done")
    def done(mid):
        msg = get_message(mid)
        msg["status"] = "done"
        log("mark_done", message=mid, patient=msg["patient"])
        return redirect(url_for("inbox", toast=f"Marked “{msg['subject']}” as done."))

    @app.post("/chart/<pid>/med/<med_id>/dose")
    def edit_dose(pid, med_id):
        patient = state["patients"].get(pid)
        if patient is None:
            abort(404)
        med = next((m for m in patient["meds"] if m["id"] == med_id), None)
        if med is None:
            abort(404)
        new_dose = request.form.get("dose", "").strip()
        old_dose = med["dose"]
        if new_dose:
            med["dose"] = new_dose
        log("edit_med", patient=pid, med=med_id, old_dose=old_dose, new_dose=new_dose)
        from_mid = request.form.get("from") or None
        return redirect(url_for("chart", pid=pid, tab="meds", **({"from": from_mid} if from_mid else {}),
                                toast="Dose updated."))

    @app.get("/__harness/state")
    def harness_state():
        # Not linked from any page; the harness reads stored state here.
        if request.headers.get("X-Harness-Token") != harness_token:
            abort(404)
        with lock:
            return jsonify(state)

    return app
