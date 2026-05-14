import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Tuple
from datetime import datetime
from .models import AppConfig

def get_html_template(title, intro_text, content):
    """
    Template HTML universal da Weepulse. 
    Recebe o Título, o Subtítulo (Intro) e o Conteúdo dinâmico de cada tela.
    """
    year = datetime.now().year
    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
    </head>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; background-color: #f4f4f9; padding: 20px; margin: 0;">
        <div style="max-width: 650px; margin: 0 auto; background: #fff; padding: 40px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05);">
            
            <div style="text-align: center; margin-bottom: 30px;">
                <img src="https://weepulse.com.br/blog/wp-content/uploads/2025/05/favicon.webp" alt="Weepulse" style="max-width: 100px; height: auto; display: inline-block;">
                <p style="color: #64748b; font-size: 14px; margin-top: 12px; font-weight: 500;">Sua Big tech em produtos TOTVS</p>
            </div>
            
            <h2 style="color: #1d4ed8; text-align: center; font-size: 24px; text-transform: uppercase; font-weight: bold; letter-spacing: 1px; margin-bottom: 15px;">{title}</h2>
            
            <p style="color: #475569; font-size: 15px; margin-bottom: 30px; text-align: center;">
                {intro_text}
            </p>
            
            {content}
            
            <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #e2e8f0; text-align: center; color: #94a3b8; font-size: 12px;">
                Enviado automaticamente por Weepulse Monitor &copy; {year}<br>
                Não responda a este e-mail.
            </div>
        </div>
    </body>
    </html>
    """

def send_alert_email(subject: str, html_content: str, title="NOTIFICAÇÃO DO SISTEMA", intro_text="Este é um relatório automático gerado pelo sistema de monitoramento.") -> Tuple[bool, str]:
    """
    Motor universal de envio de e-mails.
    """
    cfg = AppConfig.query.first()
    if not cfg or not cfg.smtp_host or not cfg.alert_email_to:
        return False, "SMTP ou e-mail de destino não configurado no banco."

    msg = MIMEMultipart()
    msg['From'] = cfg.smtp_profile if cfg.smtp_profile else cfg.smtp_user
    msg['To'] = cfg.alert_email_to
    msg['Subject'] = subject
    
    # Monta o HTML embutindo o conteúdo da tela atual dentro do Template Universal
    full_html = get_html_template(title, intro_text, html_content)
    msg.attach(MIMEText(full_html, 'html'))

    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
            if cfg.smtp_user and cfg.smtp_password:
                server.login(cfg.smtp_user, cfg.smtp_password)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            server.ehlo()
            if cfg.smtp_tls:
                server.starttls()
                server.ehlo()
            
            if cfg.smtp_user and cfg.smtp_password:
                server.login(cfg.smtp_user, cfg.smtp_password)
        
        server.send_message(msg)
        server.quit()
        return True, "E-mail enviado com sucesso! Verifique a sua caixa de entrada."
        
    except smtplib.SMTPAuthenticationError as e:
        ms_error = e.smtp_error.decode(errors='ignore') if e.smtp_error else str(e)
        return False, f"Rejeitado pelo Provedor: {ms_error}"
    except TimeoutError:
        return False, f"Timeout: O servidor demorou a responder na porta {cfg.smtp_port}."
    except Exception as e:
        return False, f"Erro SMTP: {str(e)}"