import time
import threading
import warnings
from datetime import datetime, timedelta
from .extensions import db
from .models import Server, AppConfig, ServerMetric

warnings.filterwarnings("ignore", module="winrm")

noc_cache = {
    "servers": [],
    "services": [],
    "last_update": "A aguardar a primeira varredura do motor..."
}

# Controle de tempo para não disparar 50 snapshots no mesmo minuto
SNAPSHOT_COOLDOWN_CACHE = {}

def _get_time_str(time_obj):
    if not time_obj: return None
    if hasattr(time_obj, 'strftime'): return time_obj.strftime("%H:%M")
    return str(time_obj)[:5]

def worker_loop(app):
    from .servers_monitor import collect_server_health
    from .services_scan import scan_protheus_services, run_restart_all_sequence
    from .protheus_cleaner import run_protheus_cleanup

    last_cleanup_date = None
    last_restart_all_date = None
    last_log_scan_minute = -1
    last_table_growth_hour = -1 

    with app.app_context():
        time.sleep(5)

        while True:
            try:
                cfg = AppConfig.query.first()
                servers = Server.query.all()
                now = datetime.now()
                now_str = now.strftime("%H:%M")

                if cfg:
                    # 1. LIMPEZA
                    cleanup_time_str = _get_time_str(getattr(cfg, 'cleanup_time', None))
                    if cleanup_time_str and now_str == cleanup_time_str and last_cleanup_date != now.date():
                        try:
                            run_protheus_cleanup(servers)
                        except: pass
                        finally: last_cleanup_date = now.date()

                    # 2. RESTART SEQUENCIAL
                    sched_time_val = getattr(cfg, 'scheduled_time', getattr(cfg, 'restart_time', None))
                    sched_time_str = _get_time_str(sched_time_val)
                    if sched_time_str:
                        html_day = str((now.weekday() + 1) % 7)
                        sched_days_val = getattr(cfg, 'scheduled_days', getattr(cfg, 'restart_days', ''))
                        days_configured = [d.strip() for d in str(sched_days_val).split(',') if d.strip()] if sched_days_val else []

                        if now_str == sched_time_str and last_restart_all_date != now.date():
                            if html_day in days_configured or not days_configured:
                                try:
                                    with app.test_request_context(json={'is_scheduled': True}):
                                        run_restart_all_sequence(is_scheduled=True)
                                except: pass
                            last_restart_all_date = now.date()

                # 3. RADAR DE LOGS
                if now.minute % 5 == 0 and last_log_scan_minute != now.minute:
                    last_log_scan_minute = now.minute
                    try:
                        from .protheus_logs import run_proactive_log_hunter
                        run_proactive_log_hunter(app)
                    except: pass

                # 4. SNAPSHOT DO BANCO DE DADOS (CRESCIMENTO)
                if now.hour != last_table_growth_hour:
                    last_table_growth_hour = now.hour
                    try:
                        from .report_ui import collect_table_growth_now
                        collect_table_growth_now(app)
                    except: pass

                # 5. COLETA DE MÉTRICAS REGULAR
                health_results = []
                for s in servers:
                    res = collect_server_health(s.address, s.name, cfg)
                    res['server_id'] = s.id
                    health_results.append(res)

                    metric = ServerMetric(
                        server_id=s.id,
                        cpu_percent=res.get('cpu_percent', 0.0),
                        mem_percent=res.get('mem_percent', 0.0)
                    )
                    db.session.add(metric)
                
                # ---> CORREÇÃO: FAZ O COMMIT AGORA PARA LIBERAR O BANCO DE DADOS <---
                db.session.commit()
                    
                # =========================================================
                # 6. GATILHO DA CAIXA PRETA (SNAPSHOT DE ALERTAS LENTOS)
                # =========================================================
                fresh_alerts = []
                for r in health_results:
                    if r.get('alerts'): # Se o servidor está estourando
                        now_ts = time.time()
                        last_snap = SNAPSHOT_COOLDOWN_CACHE.get(r['server_address'], 0)
                        
                        # Espera 5 minutos (300 seg) antes de tirar outro print do MESMO servidor
                        if (now_ts - last_snap) > 300: 
                            fresh_alerts.append({
                                "server": r['server_name'],
                                "reasons": r['alerts']
                            })
                            SNAPSHOT_COOLDOWN_CACHE[r['server_address']] = now_ts
                
                if fresh_alerts:
                    try:
                        from .alerts_snapshot import take_snapshot
                        take_snapshot(app, cfg, health_results, fresh_alerts)
                    except Exception as snap_err:
                        print(f"[ERRO SNAPSHOT CAIXA PRETA] {snap_err}")
                # =========================================================

                # 7. VARREDURA DE SERVIÇOS
                svc_results = []
                for s in servers:
                    svc_results.extend(scan_protheus_services(s.address, s.name))

                noc_cache["servers"] = health_results
                noc_cache["services"] = svc_results
                noc_cache["last_update"] = now.strftime("%d/%m/%Y %H:%M:%S")

                cutoff = datetime.now() - timedelta(hours=2)
                ServerMetric.query.filter(ServerMetric.timestamp < cutoff).delete()

                db.session.commit()
            except Exception as e:
                print(f"[BACKGROUND WORKER ERRO CRÍTICO] Falha na coleta: {e}")
                db.session.rollback()

            time.sleep(20)

def start_background_worker(app):
    t = threading.Thread(target=worker_loop, args=(app,), daemon=True)
    t.start()
