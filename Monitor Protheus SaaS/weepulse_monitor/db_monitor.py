import os
import subprocess
import base64
import json
import tempfile
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .models import AppConfig

dbmon_bp = Blueprint("dbmon", __name__)

def _run_ps_local(ps: str):
    """
    Executa o script criando um arquivo temporário no disco.
    Isso evita o Erro '[WinError 206]' de limite de caracteres da linha de comando.
    """
    ps_full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + ps
    
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as f:
            f.write(ps_full)
            
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=300
        )
        return r.stdout or ""
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

# =============================================================================
# SCRIPT SQL NATIVO - EXTREMAMENTE LIMPO (Sem Logo, Sem Copyright, Responsivo)
# =============================================================================
MONITOR_SQL_SCRIPT = """
SET NOCOUNT ON;

DECLARE @HorasAnalise INT = 24; 
DECLARE @TopN INT = 20;         

DECLARE @MostrarSugestoes BIT = 1;      
DECLARE @MostrarFragmentacao BIT = 1;   

DECLARE @Subject NVARCHAR(255); DECLARE @Body NVARCHAR(MAX); 
DECLARE @Header NVARCHAR(MAX); DECLARE @ServerInfo NVARCHAR(MAX); DECLARE @InfoGeral NVARCHAR(MAX);
DECLARE @Cards NVARCHAR(MAX); DECLARE @TablesSection NVARCHAR(MAX);
DECLARE @InfraSection NVARCHAR(MAX); DECLARE @AuxTables NVARCHAR(MAX);
DECLARE @MainTable NVARCHAR(MAX); 
DECLARE @Footer NVARCHAR(MAX);

DECLARE @XML_TopQueries NVARCHAR(MAX); DECLARE @XML_Waits NVARCHAR(MAX);
DECLARE @XML_MissingIndexes NVARCHAR(MAX); DECLARE @XML_Tables NVARCHAR(MAX);
DECLARE @XML_JobFailures NVARCHAR(MAX); 
DECLARE @XML_Backups NVARCHAR(MAX); DECLARE @XML_DiskSpace NVARCHAR(MAX);
DECLARE @XML_DiskLatency NVARCHAR(MAX); 
DECLARE @XML_DbSizes NVARCHAR(MAX); DECLARE @XML_Blocking NVARCHAR(MAX);
DECLARE @XML_Fragmentation NVARCHAR(MAX);

DECLARE @DeadlockCount INT = 0; DECLARE @BlockingAtual INT = 0;
DECLARE @TempDBPercent DECIMAL(5,2) = 0; DECLARE @PLE INT = 0;
DECLARE @ColorTempDB VARCHAR(20); DECLARE @ColorPLE VARCHAR(20);
DECLARE @UptimeString VARCHAR(100); 
DECLARE @DataInicio DATETIME = DATEADD(HOUR, -@HorasAnalise, GETDATE());

SET @XML_Blocking = CAST((SELECT TOP 5 'text-align:center; font-size:11px; padding:3px;' AS 'td/@style', r.session_id AS 'td', '', 'text-align:center; font-size:11px; color:#dc3545; font-weight:bold; padding:3px;' AS 'td/@style', r.blocking_session_id AS 'td', '', 'text-align:left; font-size:11px; padding:3px;' AS 'td/@style', ISNULL(s.login_name, 'N/A') AS 'td', '', 'text-align:left; font-size:10px; padding:3px;' AS 'td/@style', ISNULL(SUBSTRING(st.text, 1, 80) + '...', 'N/A') AS 'td' FROM sys.dm_exec_requests r INNER JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) st WHERE r.blocking_session_id > 0 FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));

SET @XML_DbSizes = CAST((SELECT TOP 5 'text-align:center; font-size:11px; padding:2px; font-weight:bold;' AS 'td/@style', DB_NAME(database_id) AS 'td', '', 'text-align:center; font-size:11px; padding:2px;' AS 'td/@style', CAST(SUM(CASE WHEN type_desc = 'ROWS' THEN size * 8.0 / 1024.0 / 1024.0 ELSE 0 END) AS DECIMAL(10,2)) AS 'td', '', 'text-align:center; font-size:11px; padding:2px;' AS 'td/@style', CAST(SUM(CASE WHEN type_desc = 'LOG' THEN size * 8.0 / 1024.0 / 1024.0 ELSE 0 END) AS DECIMAL(10,2)) AS 'td' FROM sys.master_files WHERE database_id > 4 GROUP BY database_id ORDER BY SUM(size) DESC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));

IF @MostrarFragmentacao = 1
BEGIN
    SET @XML_Fragmentation = CAST((SELECT TOP 5 'text-align:center; font-size:11px; padding:2px; font-weight:bold;' AS 'td/@style', DB_NAME(database_id) + '.' + OBJECT_NAME(object_id, database_id) AS 'td', '', 'text-align:center; font-size:11px; color:#dc3545; padding:2px; font-weight:bold;' AS 'td/@style', CAST(avg_fragmentation_in_percent AS DECIMAL(5,1)) AS 'td', '', 'text-align:center; font-size:11px; padding:2px;' AS 'td/@style', page_count AS 'td' FROM sys.dm_db_index_physical_stats(NULL, NULL, NULL, NULL, 'LIMITED') WHERE avg_fragmentation_in_percent > 30 AND page_count > 1000 AND index_id > 0 ORDER BY avg_fragmentation_in_percent DESC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));
END

SET @XML_DiskLatency = (SELECT '<tr style="background-color:white;"><td style="text-align:center; font-size:11px; padding:2px; font-weight:bold;">' + DB_NAME(vfs.database_id) + ' (' + LEFT(UPPER(mf.physical_name), 1) + ':)' + '</td><td style="padding:2px; font-size:11px; text-align:left; width:150px;">' + CAST(CAST(io_stall_read_ms / NULLIF(num_of_reads, 0) AS INT) AS VARCHAR) + 'ms ' + '<div style="height:10px; width:' + CAST(CASE WHEN (io_stall_read_ms / NULLIF(num_of_reads, 0)) > 100 THEN 100 ELSE (io_stall_read_ms / NULLIF(num_of_reads, 0)) END AS VARCHAR) + '%; background-color:' + CASE WHEN (io_stall_read_ms / NULLIF(num_of_reads, 0)) > 50 THEN '#dc3545' WHEN (io_stall_read_ms / NULLIF(num_of_reads, 0)) > 20 THEN '#ffc107' ELSE '#28a745' END + '; display:inline-block; border-radius:2px;"></div></td><td style="padding:2px; font-size:11px; text-align:left; width:150px;">' + CAST(CAST(io_stall_write_ms / NULLIF(num_of_writes, 0) AS INT) AS VARCHAR) + 'ms ' + '<div style="height:10px; width:' + CAST(CASE WHEN (io_stall_write_ms / NULLIF(num_of_writes, 0)) > 100 THEN 100 ELSE (io_stall_write_ms / NULLIF(num_of_writes, 0)) END AS VARCHAR) + '%; background-color:' + CASE WHEN (io_stall_write_ms / NULLIF(num_of_writes, 0)) > 50 THEN '#dc3545' WHEN (io_stall_write_ms / NULLIF(num_of_writes, 0)) > 20 THEN '#ffc107' ELSE '#28a745' END + '; display:inline-block; border-radius:2px;"></div></td></tr>' FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs JOIN sys.master_files mf ON vfs.database_id = mf.database_id AND vfs.file_id = mf.file_id WHERE num_of_reads > 0 AND num_of_writes > 0 ORDER BY (io_stall_read_ms / NULLIF(num_of_reads, 0)) DESC FOR XML PATH(''), TYPE).value('.', 'NVARCHAR(MAX)');

SET @XML_Backups = CAST((SELECT TOP 5 'text-align:center; font-size:11px; padding:2px; font-weight:bold;' AS 'td/@style', d.name AS 'td', '', 'text-align:center; font-size:11px; padding:2px;' AS 'td/@style', ISNULL(FORMAT(MAX(b.backup_finish_date), 'dd/MM HH:mm'), 'NÃO REALIZADO') AS 'td', '', 'text-align:center; font-size:11px; padding:2px; font-weight:bold; color:' + CASE WHEN MAX(b.backup_finish_date) >= @DataInicio THEN '#28a745' ELSE '#dc3545' END + ';' AS 'td/@style', CASE WHEN MAX(b.backup_finish_date) >= @DataInicio THEN 'OK' ELSE 'ATENÇÃO' END AS 'td' FROM sys.databases d LEFT JOIN msdb.dbo.backupset b ON d.name = b.database_name AND b.type = 'D' WHERE d.name NOT IN ('tempdb','model') GROUP BY d.name ORDER BY MAX(b.backup_finish_date) ASC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));
SET @XML_DiskSpace = CAST((SELECT DISTINCT 'text-align:left; font-size:11px; padding:2px;' AS 'td/@style', vs.volume_mount_point AS 'td', '', 'text-align:center; font-size:11px; padding:2px;' AS 'td/@style', CAST(vs.total_bytes / 1024.0 / 1024 / 1024 AS DECIMAL(10,0)) AS 'td', '', 'text-align:center; font-size:11px; padding:2px; font-weight:bold; color:' + CASE WHEN (vs.available_bytes * 100.0 / vs.total_bytes) < 10 THEN '#dc3545' ELSE '#28a745' END + ';' AS 'td/@style', CAST(vs.available_bytes / 1024.0 / 1024 / 1024 AS DECIMAL(10,1)) AS 'td', '', 'text-align:center; font-size:11px; padding:2px;' AS 'td/@style', CAST(vs.available_bytes * 100.0 / vs.total_bytes AS DECIMAL(5,1)) AS 'td' FROM sys.master_files f CROSS APPLY sys.dm_os_volume_stats(f.database_id, f.file_id) vs FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));
SELECT @UptimeString = CAST(DATEDIFF(MINUTE, sqlserver_start_time, GETDATE()) / 1440 AS VARCHAR) + ' dias, ' + CAST((DATEDIFF(MINUTE, sqlserver_start_time, GETDATE()) % 1440) / 60 AS VARCHAR) + 'h ' + CAST((DATEDIFF(MINUTE, sqlserver_start_time, GETDATE()) % 60) AS VARCHAR) + 'm' FROM sys.dm_os_sys_info;

IF OBJECT_ID('tempdb..#TopTabelas_Weepulse') IS NOT NULL DROP TABLE #TopTabelas_Weepulse;
CREATE TABLE #TopTabelas_Weepulse (Banco VARCHAR(100), Tabela VARCHAR(150), Linhas BIGINT, TotalGB DECIMAL(18,2));

EXEC sp_MSforeachdb 'USE [?]; IF DB_ID(''?'') > 4 BEGIN INSERT INTO #TopTabelas_Weepulse (Banco, Tabela, Linhas, TotalGB) SELECT TOP 20 DB_NAME(), t.name, SUM(CASE WHEN p.index_id IN (0, 1) THEN p.row_count ELSE 0 END), CAST(SUM(p.reserved_page_count) * 8.0 / 1024.0 / 1024.0 AS DECIMAL(18,2)) FROM sys.tables t WITH(NOLOCK) INNER JOIN sys.dm_db_partition_stats p WITH(NOLOCK) ON t.object_id = p.object_id WHERE t.is_ms_shipped = 0 GROUP BY t.name HAVING SUM(CASE WHEN p.index_id IN (0, 1) THEN p.row_count ELSE 0 END) > 0 ORDER BY SUM(p.reserved_page_count) DESC END';

SET @XML_Tables = CAST((SELECT TOP 10 'text-align:center; font-size:11px; padding:3px; font-weight:bold;' AS 'td/@style', Banco + '.' + Tabela AS 'td', '', 'text-align:center; font-size:11px; padding:3px;' AS 'td/@style', FORMAT(Linhas, 'N0') AS 'td', '', 'text-align:center; font-size:11px; padding:3px;' AS 'td/@style', TotalGB AS 'td' FROM #TopTabelas_Weepulse ORDER BY TotalGB DESC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));

SET @XML_JobFailures = CAST((SELECT TOP 5 'text-align:left; font-size:11px; color:#dc3545;' AS 'td/@style', j.name AS 'td', '', 'text-align:center; font-size:11px;' AS 'td/@style', FORMAT(CONVERT(DATETIME, RTRIM(h.run_date) + ' ' + STUFF(STUFF(RIGHT('000000' + RTRIM(h.run_time), 6), 5, 0, ':'), 3, 0, ':')), 'dd/MM HH:mm') AS 'td', '', 'text-align:left; font-size:10px; word-wrap: break-word;' AS 'td/@style', h.message AS 'td' FROM msdb.dbo.sysjobhistory h INNER JOIN msdb.dbo.sysjobs j ON h.job_id = j.job_id WHERE h.run_status = 0 AND CONVERT(DATETIME, RTRIM(h.run_date) + ' ' + STUFF(STUFF(RIGHT('000000' + RTRIM(h.run_time), 6), 5, 0, ':'), 3, 0, ':')) >= @DataInicio ORDER BY h.run_date DESC, h.run_time DESC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));

SET @XML_Waits = CAST((SELECT TOP (5) 'text-align:center; padding:5px; font-weight:bold;' AS 'td/@style', wait_type AS 'td', '', 'text-align:center; padding:5px;' AS 'td/@style', CAST(wait_time_ms / 1000.0 AS DECIMAL(20, 2)) AS 'td', '', 'text-align:center; padding:5px;' AS 'td/@style', CAST(100.0 * wait_time_ms / SUM(wait_time_ms) OVER() AS DECIMAL(5,2)) AS 'td', '', 'text-align:left; padding:5px; font-size:10px; color:#666;' AS 'td/@style', CASE WHEN wait_type LIKE 'LCK%' THEN 'Bloqueio.' WHEN wait_type LIKE 'PAGEIOLATCH%' THEN 'Disco (Leitura).' WHEN wait_type LIKE 'WRITELOG' THEN 'Disco (Log).' WHEN wait_type LIKE 'SOS_SCHEDULER_YIELD' THEN 'CPU.' WHEN wait_type LIKE 'CXPACKET' THEN 'Paralelismo.' WHEN wait_type LIKE 'ASYNC_NETWORK_IO' THEN 'Rede/App.' ELSE 'Outros' END AS 'td' FROM sys.dm_os_wait_stats WHERE wait_type NOT IN ('DIRTY_PAGE_POLL', 'HADR_FILESTREAM_IOMGR_IOCOMPLETION', 'LOGMGR_QUEUE', 'ONDEMAND_TASK_QUEUE', 'REQUEST_FOR_DEADLOCK_SEARCH', 'SLEEP_TASK', 'SLEEP_SYSTEMTASK', 'SQLTRACE_BUFFER_FLUSH', 'WAITFOR', 'CHECKPOINT_QUEUE', 'LAZYWRITER_SLEEP', 'XE_TIMER_EVENT', 'XE_DISPATCHER_WAIT', 'FT_IFTS_SCHEDULER_IDLE_WAIT', 'BROKER_TO_FLUSH', 'BROKER_TASK_STOP', 'BROKER_RECEIVE_WAITFOR', 'BROKER_EVENTHANDLER','SOS_WORK_DISPATCHER', 'DISPATCHER_QUEUE_SEMAPHORE', 'CLR_AUTO_EVENT', 'CLR_MANUAL_EVENT', 'QDS_PERSIST_TASK_MAIN_LOOP_SLEEP', 'QDS_ASYNC_QUEUE','PWAIT_EXTENSIBILITY_CLEANUP_TASK', 'SP_SERVER_DIAGNOSTICS_SLEEP', 'SQLTRACE_INCREMENTAL_FLUSH_SLEEP') ORDER BY wait_time_ms DESC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));

IF @MostrarSugestoes = 1
BEGIN
    IF OBJECT_ID('tempdb..#TmpSugestoes') IS NOT NULL DROP TABLE #TmpSugestoes;
    CREATE TABLE #TmpSugestoes (
        ID INT IDENTITY(1,1), DB_ID INT, DB_Name NVARCHAR(128), Obj_ID INT, TableName NVARCHAR(128),
        Statement NVARCHAR(MAX), EqCols NVARCHAR(MAX), IneqCols NVARCHAR(MAX), IncCols NVARCHAR(MAX),
        Impact DECIMAL(5,2), SuggestedName NVARCHAR(128)
    );

    INSERT INTO #TmpSugestoes (DB_ID, DB_Name, Obj_ID, TableName, Statement, EqCols, IneqCols, IncCols, Impact)
    SELECT TOP (5) 
        d.database_id, DB_NAME(d.database_id), d.object_id, OBJECT_NAME(d.object_id, d.database_id), 
        d.statement, d.equality_columns, d.inequality_columns, d.included_columns, s.avg_user_impact
    FROM sys.dm_db_missing_index_details d 
    INNER JOIN sys.dm_db_missing_index_groups g ON d.index_handle = g.index_handle 
    INNER JOIN sys.dm_db_missing_index_group_stats s ON g.index_group_handle = s.group_handle 
    WHERE s.avg_user_impact > 70.0 
    ORDER BY s.avg_total_user_cost * s.avg_user_impact * s.user_seeks DESC;

    DECLARE @i_sug INT = 1, @max_i_sug INT = (SELECT MAX(ID) FROM #TmpSugestoes);
    DECLARE @DynSQL_Sug NVARCHAR(MAX), @CurrDB_Sug NVARCHAR(128), @CurrObj_Sug INT, @CurrTable_Sug NVARCHAR(128);
    DECLARE @MaxName_Sug NVARCHAR(128), @NextChar_Sug VARCHAR(10), @Pos_Sug INT;

    WHILE @i_sug <= @max_i_sug
    BEGIN
        SELECT @CurrDB_Sug = DB_Name, @CurrObj_Sug = Obj_ID, @CurrTable_Sug = TableName FROM #TmpSugestoes WHERE ID = @i_sug;
        SET @MaxName_Sug = NULL;
        SET @DynSQL_Sug = N'SELECT TOP 1 @outMaxName = name FROM [' + @CurrDB_Sug + N'].sys.indexes WHERE object_id = @pObj AND name LIKE @pTable + ''[1-9A-Z]'' AND LEN(name) = LEN(@pTable) + 1 ORDER BY CHARINDEX(RIGHT(name, 1), ''123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'') DESC';
        EXEC sp_executesql @DynSQL_Sug, N'@pObj INT, @pTable NVARCHAR(128), @outMaxName NVARCHAR(128) OUTPUT', @pObj = @CurrObj_Sug, @pTable = @CurrTable_Sug, @outMaxName = @MaxName_Sug OUTPUT;

        IF @MaxName_Sug IS NULL SET @NextChar_Sug = '1';
        ELSE BEGIN
            SET @Pos_Sug = CHARINDEX(RIGHT(@MaxName_Sug, 1), '123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ');
            IF @Pos_Sug > 0 AND @Pos_Sug < 35 SET @NextChar_Sug = SUBSTRING('123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', @Pos_Sug + 1, 1);
            ELSE SET @NextChar_Sug = '_FULL'; 
        END

        UPDATE #TmpSugestoes SET SuggestedName = @CurrTable_Sug + @NextChar_Sug WHERE ID = @i_sug;
        SET @i_sug = @i_sug + 1;
    END

    SET @XML_MissingIndexes = CAST((SELECT 'text-align:center; font-size:11px; padding:2px; font-weight:bold;' AS 'td/@style', OBJECT_NAME(Obj_ID, DB_ID) AS 'td', '', 'padding: 2px;' AS 'td/@style', (SELECT 'width: 100%; height: 45px; overflow: hidden; font-family: Consolas, Monospace; font-size: 9px; border: 1px solid #ccc; background-color: #fff; color: #007bff;' AS '@style', 'readonly' AS '@readonly', 'CREATE NONCLUSTERED INDEX [' + SuggestedName + '] ON ' + Statement + ' (' + ISNULL(EqCols, '') + CASE WHEN EqCols IS NOT NULL AND IneqCols IS NOT NULL THEN ', ' ELSE '' END + ISNULL(IneqCols, '') + ') ' + ISNULL('INCLUDE (' + IncCols + ')', '') + ';' FOR XML PATH('textarea'), TYPE) AS 'td', '', 'text-align:center; font-weight:bold; font-size:11px;' AS 'td/@style', CAST(Impact AS DECIMAL(5,2)) AS 'td' FROM #TmpSugestoes ORDER BY ID ASC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));
END

SET @XML_TopQueries = CAST((SELECT TOP (@TopN) 'text-align:left; font-size:11px; vertical-align: top; color:#0056b3; font-weight:bold;' AS 'td/@style', ISNULL(DB_NAME(CAST(pa.value AS INT)), ISNULL(DB_NAME(st.dbid), 'Ad-Hoc')) AS 'td', '', 'text-align:left; font-size:11px; vertical-align: top; color:#333;' AS 'td/@style', ISNULL(OBJECT_NAME(st.objectid, st.dbid), 'Ad-Hoc Query') AS 'td', '', 'padding: 5px; vertical-align: top;' AS 'td/@style', (SELECT 'width: 100%; height: 60px; overflow: auto; font-family: Consolas, Monospace; font-size: 11px; border: 1px solid #e0e0e0; background-color: #f9f9f9; color: #333; resize: vertical;' AS '@style', 'readonly' AS '@readonly', st.text AS '*' FOR XML PATH('textarea'), TYPE) AS 'td', '', 'text-align:center; vertical-align: top;' AS 'td/@style', qs.execution_count AS 'td', '', 'text-align:center; vertical-align: top; font-weight:bold; color:#555;' AS 'td/@style', FORMAT(qs.total_logical_reads, 'N0') AS 'td', '', 'text-align:center; vertical-align: top;' AS 'td/@style', CAST(qs.total_elapsed_time / 1000000.0 AS DECIMAL(20, 2)) AS 'td', '', 'text-align:center; font-weight:bold; vertical-align: top;' AS 'td/@style', CAST((qs.total_elapsed_time / qs.execution_count) / 1000000.0 AS DECIMAL(20, 4)) AS 'td', '', 'text-align:center; vertical-align: top;' AS 'td/@style', FORMAT(qs.last_execution_time, 'dd/MM/yyyy HH:mm') AS 'td' FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st OUTER APPLY sys.dm_exec_plan_attributes(qs.plan_handle) pa WHERE qs.last_execution_time >= @DataInicio AND pa.attribute = 'dbid' ORDER BY qs.total_elapsed_time DESC FOR XML PATH('tr'), ELEMENTS) AS NVARCHAR(MAX));
SELECT @PLE = cntr_value FROM sys.dm_os_performance_counters WHERE object_name LIKE '%Buffer Manager%' AND counter_name = 'Page life expectancy'; SET @ColorPLE = '#28a745'; IF @PLE < 300 SET @ColorPLE = '#dc3545'; IF @PLE BETWEEN 300 AND 600 SET @ColorPLE = '#ffc107'; 
IF OBJECT_ID('tempdb..#DeadlockEvents') IS NOT NULL DROP TABLE #DeadlockEvents; ;WITH SystemHealth AS (SELECT CAST(target_data AS XML) AS SessionXML FROM sys.dm_xe_session_targets st JOIN sys.dm_xe_sessions s ON s.address = st.event_session_address WHERE s.name = 'system_health') SELECT @DeadlockCount = COUNT(*) FROM (SELECT XEvent.value('(@timestamp)[1]', 'datetime') AS DeadlockTime FROM (SELECT CAST(event_data AS XML) AS XEventData FROM sys.fn_xe_file_target_read_file('system_health*.xel', null, null, null)) AS Data CROSS APPLY XEventData.nodes('//event') AS T(XEvent) WHERE XEvent.value('(@name)[1]', 'varchar(128)') = 'xml_deadlock_report') AS Deadlocks WHERE DeadlockTime >= @DataInicio;
SELECT @BlockingAtual = COUNT(*) FROM sys.dm_os_waiting_tasks WHERE blocking_session_id IS NOT NULL; SELECT @TempDBPercent = (SUM(allocated_extent_page_count) * 1.0 / SUM(total_page_count)) * 100 FROM tempdb.sys.dm_db_file_space_usage; SET @ColorTempDB = '#28a745'; IF @TempDBPercent > 70 SET @ColorTempDB = '#ffc107'; IF @TempDBPercent > 90 SET @ColorTempDB = '#dc3545'; 

-- ====================================================================================
-- MONTAGEM DO HTML SUPER LIMPO
-- ====================================================================================

-- Cabeçalho abre a Div principal e a Tabela. 100% responsivo.
SET @Header = N'<div style="background-color: #ffffff; font-family: Arial, sans-serif; padding: 10px; border-radius: 8px;"><table border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse;"><tr><td align="center" style="padding: 10px 0; border-bottom: 1px solid #eee;"><p style="color: #64748b; font-size: 12px; margin:0; font-weight: bold;">Período Analisado: Últimas ' + CAST(@HorasAnalise AS VARCHAR) + ' horas</p></td></tr>';

SET @ServerInfo = N'<tr><td style="padding: 10px 30px; background-color: #f8fafc; border-top: 1px solid #ddd; text-align: center;"><span style="font-size: 14px; color: #333; font-weight: bold;">&#128187; Servidor: </span><span style="font-size: 14px; color: #0056b3; margin-right: 25px;">' + CAST(SERVERPROPERTY('MachineName') AS VARCHAR) + '</span><span style="font-size: 14px; color: #333; font-weight: bold;">&#128193; Versão do SQL: </span><span style="font-size: 14px; color: #0056b3;">' + CAST(SERVERPROPERTY('ProductVersion') AS VARCHAR) + ' (' + CAST(SERVERPROPERTY('ProductLevel') AS VARCHAR) + ')</span></td></tr>';

SET @InfoGeral = N'<tr><td style="padding: 10px 30px; background-color: #f8fafc; border-top: 1px solid #fff; border-bottom: 1px solid #ddd; text-align: center;"><span style="font-size: 14px; color: #333; font-weight: bold;">&#9201; Tempo de Disponibilidade (Uptime): </span><span style="font-size: 14px; color: #0056b3;">' + @UptimeString + '</span></td></tr>';

SET @Cards = N'<tr><td style="padding: 20px;"><table width="100%" style="border-collapse: separate; border-spacing: 10px;"><tr><td width="25%" style="background-color: #f8f9fa; padding: 10px; border-radius: 5px; text-align: center; border: 1px solid #ddd;"><span style="font-size: 11px; color: #666; font-weight: bold;">MEMÓRIA (PLE)</span><br><span style="font-size: 20px; font-weight: bold; color: ' + @ColorPLE + ';">' + CAST(@PLE AS VARCHAR) + 's</span></td><td width="25%" style="background-color: #f8f9fa; padding: 10px; border-radius: 5px; text-align: center; border: 1px solid #ddd;"><span style="font-size: 11px; color: #666; font-weight: bold;">USO TEMPDB</span><br><span style="font-size: 20px; font-weight: bold; color: ' + @ColorTempDB + ';">' + CAST(@TempDBPercent AS VARCHAR) + '%</span></td><td width="25%" style="background-color: #f8f9fa; padding: 10px; border-radius: 5px; text-align: center; border: 1px solid #ddd;"><span style="font-size: 11px; color: #666; font-weight: bold;">DEADLOCKS</span><br><span style="font-size: 20px; font-weight: bold; color: ' + CASE WHEN @DeadlockCount > 0 THEN '#dc3545' ELSE '#28a745' END + ';">' + CAST(@DeadlockCount AS VARCHAR) + '</span></td><td width="25%" style="background-color: #f8f9fa; padding: 10px; border-radius: 5px; text-align: center; border: 1px solid #ddd;"><span style="font-size: 11px; color: #666; font-weight: bold;">BLOQUEIOS (AGORA)</span><br><span style="font-size: 20px; font-weight: bold; color: ' + CASE WHEN @BlockingAtual > 0 THEN '#dc3545' ELSE '#28a745' END + ';">' + CAST(@BlockingAtual AS VARCHAR) + '</span></td></tr></table></td></tr>';

SET @TablesSection = N'<tr><td style="padding: 0 30px 20px 30px;"><table width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td><h3 style="color: #0056b3; font-family: Arial, sans-serif; border-bottom: 2px solid #0056b3; padding-bottom: 5px;">&#128680; Alertas Recentes (24h)</h3><div style="margin-bottom: 10px;"><strong style="font-size: 11px; color: #555;">&#9889; Processos Bloqueados (Neste Exato Momento):</strong><table width="100%" border="1" cellpadding="3" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 11px;"><tr style="background-color: #0056b3; color: #ffffff;"><th width="10%">SPID</th><th width="15%">Bloqueado Por</th><th width="15%">Usuário</th><th width="60%">Query Travada</th></tr>' + ISNULL(@XML_Blocking, '<tr><td colspan="4" style="text-align:center; color:#28a745; font-weight:bold;">Nenhum bloqueio detectado. ✅</td></tr>') + N'</table></div><div style="margin-bottom: 10px;"><strong style="font-size: 11px; color: #555;">Jobs com Falha:</strong><table width="100%" border="1" cellpadding="3" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 11px;"><tr style="background-color: #0056b3; color: #ffffff;"><th>Job</th><th>Data</th><th>Erro</th></tr>' + ISNULL(@XML_JobFailures, '<tr><td colspan="3" style="text-align:center; color:#28a745;">Nenhum falhou. ✅</td></tr>') + N'</table></div></td></tr></table></td></tr>';

SET @InfraSection = N'<tr><td style="padding: 0 30px 20px 30px;"><h3 style="color: #0056b3; font-family: Arial, sans-serif; border-bottom: 2px solid #0056b3; padding-bottom: 5px;">&#128187; Infraestrutura e Segurança</h3><table width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td style="padding-bottom: 15px;"><h4 style="color: #333; margin-bottom: 5px; font-size: 11px; border-bottom: 1px solid #ccc;">Backups (Full)</h4><table width="100%" border="1" cellpadding="2" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 10px;"><tr style="background-color: #0056b3; color: #fff;"><th>Banco</th><th>Último</th><th>Status</th></tr>' + ISNULL(@XML_Backups, '<tr><td colspan="3">N/A</td></tr>') + N'</table></td></tr><tr><td style="padding-bottom: 15px;"><h4 style="color: #333; margin-bottom: 5px; font-size: 11px; border-bottom: 1px solid #ccc;">&#128193; Tamanho dos Bancos (Top 5)</h4><table width="100%" border="1" cellpadding="2" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 10px;"><tr style="background-color: #0056b3; color: #fff;"><th width="40%">Banco</th><th width="30%">Dados (MDF) - GB</th><th width="30%">Log (LDF) - GB</th></tr>' + ISNULL(@XML_DbSizes, '<tr><td colspan="3">N/A</td></tr>') + N'</table></td></tr><tr><td style="padding-bottom: 15px;"><h4 style="color: #333; margin-bottom: 5px; font-size: 11px; border-bottom: 1px solid #ccc;">Espaço em Disco</h4><table width="100%" border="1" cellpadding="2" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 10px;"><tr style="background-color: #0056b3; color: #fff;"><th>Drive</th><th>Tot(GB)</th><th>Livre(GB)</th><th>%</th></tr>' + ISNULL(@XML_DiskSpace, '<tr><td colspan="4">N/A</td></tr>') + N'</table></td></tr><tr><td style="padding-bottom: 15px;"><h4 style="color: #333; margin-bottom: 5px; font-size: 11px; border-bottom: 1px solid #ccc;">Latência IO (ms)</h4><table width="100%" border="1" cellpadding="2" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 10px;"><tr style="background-color: #0056b3; color: #fff;"><th>Banco</th><th>Read</th><th>Write</th></tr>' + ISNULL(@XML_DiskLatency, '<tr><td colspan="3">N/A</td></tr>') + N'</table></td></tr></table></td></tr>';

SET @AuxTables = N'<tr><td style="padding: 0 30px 20px 30px;"><h3 style="color: #0056b3; font-family: Arial, sans-serif; border-bottom: 2px solid #0056b3; padding-bottom: 5px;">&#128269; Análise de Tabelas e Índices</h3><table width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td style="padding-bottom: 20px;"><h4 style="color: #333; margin-bottom: 5px; border-bottom: 1px solid #ccc;">Top 5 Gargalos (Waits)</h4><table width="100%" border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 11px;"><tr style="background-color: #0056b3; color: #ffffff;"><th width="25%">Tipo</th><th width="20%">Tempo (s)</th><th width="15%">%</th><th width="40%">Diagnóstico</th></tr>' + ISNULL(@XML_Waits, '<tr><td colspan="4">Sem gargalos.</td></tr>') + N'</table></td></tr>';

IF @MostrarFragmentacao = 1 BEGIN SET @AuxTables = @AuxTables + N'<tr><td style="padding-bottom: 20px;"><h4 style="color: #333; margin-bottom: 5px; border-bottom: 1px solid #ccc;">Índices Críticos (>30% Fragmentação)</h4><table width="100%" border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 11px;"><tr style="background-color: #0056b3; color: #ffffff;"><th width="40%">Banco.Tabela.Índice</th><th width="30%">Fragmentação (%)</th><th width="30%">Páginas</th></tr>' + ISNULL(@XML_Fragmentation, '<tr><td colspan="3" style="text-align:center; color:#28a745;">Nenhum índice severamente fragmentado. ✅</td></tr>') + N'</table></td></tr>'; END
IF @MostrarSugestoes = 1 BEGIN SET @AuxTables = @AuxTables + N'<tr><td style="padding-bottom: 20px;"><h4 style="color: #333; margin-bottom: 5px; border-bottom: 1px solid #ccc;">Top 5 Sugestões Índices (Impacto > 70%)</h4><table width="100%" border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 11px;"><tr style="background-color: #0056b3; color: #ffffff;"><th width="20%">Tabela</th><th width="65%">Script (Copiar)</th><th width="15%">Melhora</th></tr>' + ISNULL(@XML_MissingIndexes, '<tr><td colspan="3" style="text-align:center; color:#28a745;">Banco de dados otimizado. Nenhuma sugestão crítica. ✅</td></tr>') + N'</table></td></tr>'; END

SET @AuxTables = @AuxTables + N'<tr><td><h4 style="color: #333; margin-bottom: 5px; border-bottom: 1px solid #ccc;">Top 10 Tabelas (Volumetria)</h4><table width="100%" border="1" cellpadding="3" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 11px;"><tr style="background-color: #0056b3; color: #ffffff;"><th>Tabela</th><th>Linhas</th><th>GB</th></tr>' + ISNULL(@XML_Tables, '<tr><td colspan="3">Dados não disponíveis.</td></tr>') + N'</table></td></tr></table></td></tr>';

SET @MainTable = N'<tr><td style="padding: 20px 30px 40px 30px;"><h3 style="color: #0056b3; font-family: Arial, sans-serif; border-bottom: 2px solid #0056b3; padding-bottom: 5px;">Top ' + CAST(@TopN AS VARCHAR) + ' Queries Mais Lentas</h3><table width="100%" border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; border-color: #eee; font-family: Arial, sans-serif; font-size: 12px;"><tr style="background-color: #0056b3; color: #ffffff;"><th style="text-align: left; width: 10%;">Banco</th><th style="text-align: left; width: 10%;">Objeto</th><th style="text-align: left; width: 37%;">Query</th><th style="text-align: center; width: 5%;">Qtd</th><th style="text-align: center; width: 10%;">IO</th><th style="text-align: center; width: 9%;">Total(s)</th><th style="text-align: center; width: 9%;">Méd(s)</th><th style="text-align: center; width: 10%;">Última</th></tr>' + ISNULL(@XML_TopQueries, '<tr><td colspan="8">Nenhuma query crítica.</td></tr>') + N'</table></td></tr>';

-- Rodapé fecha a tabela e a div principal
SET @Footer = N'</table></div>';

-- Montagem Final sem os elementos antigos
SET @Body = CAST(@Header AS NVARCHAR(MAX)) + CAST(@ServerInfo AS NVARCHAR(MAX)) + CAST(@InfoGeral AS NVARCHAR(MAX)) + CAST(@Cards AS NVARCHAR(MAX)) + CAST(@TablesSection AS NVARCHAR(MAX)) + CAST(@InfraSection AS NVARCHAR(MAX)) + CAST(@AuxTables AS NVARCHAR(MAX)) + CAST(@MainTable AS NVARCHAR(MAX)) + CAST(@Footer AS NVARCHAR(MAX));

SELECT @Body AS ReportHTML;
"""

@dbmon_bp.route("/", methods=["GET"])
@login_required
def db_monitor_home():
    return render_template("pages/db_monitor.html")

@dbmon_bp.route("/run", methods=["POST"])
@login_required
def run_monitor():
    cfg = AppConfig.query.first()
    
    if not cfg or not cfg.sql_host or not cfg.sql_user:
        return jsonify({
            "ok": False, 
            "html": "<div style='color:#ef4444; padding:20px; font-size:16px; font-weight:bold;'>❌ Banco de dados não configurado. Por favor, acesse o Menu 6 (Configurações) e salve as credenciais SQL.</div>"
        })

    db_name = cfg.sql_database if cfg.sql_database else "master"
    
    # Codificando em Base64 para proteger as aspas do SQL
    sql_b64 = base64.b64encode(MONITOR_SQL_SCRIPT.encode('utf-8')).decode('utf-8')

    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    $connString = "Server={cfg.sql_host};Database={db_name};User Id={cfg.sql_user};Password={cfg.sql_password};TrustServerCertificate=True;Connection Timeout=15;"
    $sql = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("{sql_b64}"))

    try {{
        $conn = New-Object System.Data.SqlClient.SqlConnection($connString)
        $conn.Open()
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $sql
        $cmd.CommandTimeout = 300  
        
        $result = $cmd.ExecuteScalar()
        $conn.Close()

        Write-Output "===HTML-START==="
        Write-Output $result
        Write-Output "===HTML-END==="
    }} catch {{
        Write-Output "===HTML-START==="
        Write-Output "<div style='color:#ef4444; padding:20px; font-family: monospace;'><b>Falha na execução do SQL:</b><br>$($_.Exception.Message)</div>"
        Write-Output "===HTML-END==="
    }}
    """

    out = _run_ps_local(ps_script)

    start_idx = out.find("===HTML-START===")
    end_idx = out.find("===HTML-END===")

    if start_idx != -1 and end_idx != -1:
        html_content = out[start_idx + 16:end_idx].strip()
        return jsonify({"ok": True, "html": html_content})

    return jsonify({"ok": False, "html": f"<div style='color:#ef4444; padding:20px;'><b>Erro desconhecido ao comunicar com o servidor:</b><br>{out}</div>"})