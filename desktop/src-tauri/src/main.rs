#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use tauri::Manager;

fn bridge_paths(app: &tauri::AppHandle) -> Result<(PathBuf, PathBuf), String> {
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let resources = app.path().resource_dir().ok().map(|path| path.join("python"));
    let packaged = resources.filter(|path| path.join("bot/apps/desktop.py").is_file());

    if let Some(packaged) = packaged.filter(|_| !cfg!(debug_assertions)) {
        let data = app.path().app_local_data_dir().map_err(|error| error.to_string())?;
        Ok((packaged, data.join("local-chat.sqlite3")))
    } else if source.join("bot/apps/desktop.py").is_file() {
        Ok((source.clone(), source.join("runtime/local-chat.sqlite3")))
    } else {
        Err("找不到 Ohana 的 Python 后端".into())
    }
}

fn run_bridge(source: PathBuf, database: PathBuf, request: Value) -> Result<Value, String> {
    let python = std::env::var_os("OHANA_PYTHON").unwrap_or_else(|| "python".into());
    let mut command = Command::new(python);
    command
        .args(["-B", "-m", "bot.apps.desktop", "--db"])
        .arg(database)
        .current_dir(source)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let mut child = command.spawn().map_err(|error| format!("无法启动 Python 后端：{error}"))?;
    let input = serde_json::to_vec(&request).map_err(|error| error.to_string())?;
    let written = child
        .stdin
        .take()
        .ok_or("无法打开 Python 输入管道")?
        .write_all(&input);
    let output = child.wait_with_output().map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err("Python 后端退出。请核对 Python 3.11+ 和 OHANA_PYTHON 配置".into());
    }
    written.map_err(|error| error.to_string())?;
    serde_json::from_slice(&output.stdout).map_err(|_| "Python 后端返回了无效数据".into())
}

#[tauri::command]
async fn desktop_request(app: tauri::AppHandle, action: String, payload: Value) -> Result<Value, String> {
    if !matches!(action.as_str(), "load" | "send" | "save_app" | "open_chat" | "restore_chat") {
        return Err("未知的桌面操作".into());
    }
    let request = json!({"action": action, "payload": payload});
    let (source, default_database) = bridge_paths(&app)?;
    let database = std::env::var_os("OHANA_DESKTOP_DB")
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .unwrap_or(default_database);
    tauri::async_runtime::spawn_blocking(move || run_bridge(source, database, request))
        .await
        .map_err(|error| error.to_string())?
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![desktop_request])
        .run(tauri::generate_context!())
        .expect("Ohana desktop failed to start");
}
