import win32serviceutil
import win32service
import win32event
import servicemanager
import sys
import os
import datetime

# Garante que o serviço rode na pasta correta (raiz do projeto)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
os.chdir(BASE_DIR)

# Classe para redirecionar o console para o arquivo consoleapp.log
class Logger:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")

    def write(self, message):
        self.log.write(message)
        self.log.flush() # Força a gravação imediata no arquivo

    def flush(self):
        self.log.flush()

class WeepulseService(win32serviceutil.ServiceFramework):
    # =========================================================
    # CONFIGURAÇÕES DO SERVIÇO NO WINDOWS
    # =========================================================
    _svc_name_ = ".WeepulseMonitor"
    _svc_display_name_ = ".Weepulse - Monitoramento Protheus"
    _svc_description_ = "Serviço Web para monitoramento, coleta de métricas e Auto-Healing do ambiente TOTVS Protheus."

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ''))
        self.main()

    def main(self):
        # 1. Configura a gravação de logs
        log_path = os.path.join(BASE_DIR, "consoleapp.log")
        sys.stdout = Logger(log_path)
        sys.stderr = sys.stdout # Redireciona erros (tracebacks) para o mesmo arquivo

        print(f"\n[{datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] === INICIANDO SERVIÇO WEEPULSE ===")
        
        try:
            # 2. Importa e inicia o App Flask
            from weepulse_monitor import create_app
            from weepulse_monitor.extensions import db
            
            app = create_app()
            
            with app.app_context():
                db.create_all()
                
            # IMPORTANTE: use_reloader=False é OBRIGATÓRIO em serviços do Windows
            # debug=False também é recomendado para não travar o serviço
            app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
            
        except Exception as e:
            print(f"[ERRO CRÍTICO] Falha ao iniciar a aplicação: {e}")

if __name__ == '__main__':
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(WeepulseService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(WeepulseService)