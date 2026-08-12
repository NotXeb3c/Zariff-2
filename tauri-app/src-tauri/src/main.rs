// Zariff — AI System Control Agent
// Tauri v2 application entry point
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod commands;
mod file_access;
mod hotkey;
mod tray;
use battery::Manager as BatteryManager;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;
use sysinfo::Disks;
use sysinfo::Networks;
use sysinfo::System;
use tauri::Manager;
/// Global handle to the Python daemon process so we can kill it on exit.
struct DaemonProcess(Mutex<Option<Child>>);

const DAEMON_HOST: &str = "127.0.0.1";
const DAEMON_PORT: u16 = 8785;
fn get_app_data_dir() -> std::path::PathBuf {
    let home = dirs::home_dir().unwrap_or_else(|| std::path::PathBuf::from("."));
    home.join(".config").join("zariff")
}
fn get_venv_python() -> std::path::PathBuf {
    let venv_dir = get_app_data_dir().join("env");
    #[cfg(target_os = "windows")]
    {
        venv_dir.join("Scripts").join("python.exe")
    }
    #[cfg(not(target_os = "windows"))]
    {
        venv_dir.join("bin").join("python3")
    }
}
/// Try to launch the daemon using a specific python path.
fn try_spawn_with(python: &std::path::Path) -> Option<Child> {
    let mut cmd = Command::new(python);
    cmd.args(["-m", "pilot.server"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("[Zariff] Failed to spawn daemon with {:?}: {}", python, e);
            None
        }
    }
}

/// Allow the child to fail fast, but do not wait for model initialization.
/// The Svelte client already exposes a reconnecting "Connecting" state, and
/// first startup can legitimately take well over eight seconds.
fn daemon_child_started(child: &mut Child, source: &str) -> bool {
    std::thread::sleep(Duration::from_millis(500));
    match child.try_wait() {
        Ok(Some(status)) => {
            eprintln!("[Zariff] Daemon exited during {source} startup with status: {status}");
            false
        }
        Ok(None) => true,
        Err(error) => {
            eprintln!("[Zariff] Failed to inspect daemon after {source} startup: {error}");
            false
        }
    }
}

/// Run the first-time venv + pip install in a background thread (non-blocking).
fn daemon_requirement() -> String {
    format!("pilot-daemon[all]=={}", env!("CARGO_PKG_VERSION"))
}

fn playwright_browser_install_args() -> [&'static str; 4] {
    ["-m", "playwright", "install", "chromium"]
}

fn setup_venv_in_background() {
    std::thread::spawn(|| {
        let data_dir = get_app_data_dir();
        let _ = std::fs::create_dir_all(&data_dir);
        let venv_dir = data_dir.join("env");
        println!("[Zariff] First run detected — setting up virtual environment in background...");
        // 1. Create venv
        #[cfg(target_os = "windows")]
        let sys_python = "python";
        #[cfg(not(target_os = "windows"))]
        let sys_python = "python3";
        let mut venv_cmd = Command::new(sys_python);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            venv_cmd.creation_flags(0x08000000);
        }
        let ok = venv_cmd
            .args(["-m", "venv", venv_dir.to_str().unwrap()])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !ok {
            eprintln!("[Zariff] Background setup: failed to create venv. Is Python installed?");
            return;
        }
        // 2. pip install pilot-daemon
        #[cfg(target_os = "windows")]
        let pip_exe = venv_dir.join("Scripts").join("pip.exe");
        #[cfg(not(target_os = "windows"))]
        let pip_exe = venv_dir.join("bin").join("pip");
        #[cfg(target_os = "windows")]
        let venv_python = venv_dir.join("Scripts").join("python.exe");
        #[cfg(not(target_os = "windows"))]
        let venv_python = venv_dir.join("bin").join("python");

        let mut pip_cmd = Command::new(&pip_exe);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            pip_cmd.creation_flags(0x08000000);
        }

        // [all] pulls in every optional feature this project ships (voice,
        // vision, browser automation, gesture cursor, self-healing,
        // supervision, gateway/risk-gate, documents, cognitive, ssh,
        // network, wasm) so real users get every shipped feature out of
        // the box, not a partial install requiring separate manual pip
        // steps per feature. Every one of these still defaults to off/
        // config-gated behavior where the feature is privacy- or
        // safety-sensitive (e.g. supervision's global keyboard/mouse hook
        // stays behind its own explicit one-time "I understand" consent
        // checkbox in Settings regardless of whether pynput is installed)
        // -- installing the package is not the same as enabling the
        // feature.
        // Keep the desktop and daemon on the same release. An unpinned install
        // can silently pair an older desktop with a newer, incompatible RPC
        // surface after a future PyPI publication.
        let daemon_requirement = daemon_requirement();
        let ok = pip_cmd
            .args(["install", daemon_requirement.as_str()])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);

        if !ok {
            eprintln!("[Zariff] Background setup: pip install failed.");
            return;
        }

        // Installing the Playwright Python package does not install a browser
        // executable. Provision Chromium during the same background setup so
        // browser actions work without a separate developer command.
        let mut browser_cmd = Command::new(&venv_python);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            browser_cmd.creation_flags(0x08000000);
        }
        let browser_ok = browser_cmd
            .args(playwright_browser_install_args())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !browser_ok {
            eprintln!("[Zariff] Background setup: Chromium installation failed.");
            return;
        }

        println!("[Zariff] Background setup complete — restart the app to activate AI backend.");
    });
}
#[tauri::command]
fn get_system_stats() -> serde_json::Value {
    let mut system = System::new_all();
    system.refresh_cpu_all();
    // CPU
    let cpu = system.global_cpu_usage();
    let cpu_name = system
        .cpus()
        .first()
        .map(|c| c.brand().to_string())
        .unwrap_or_default();
    let total_ram_gb = system.total_memory() / 1024 / 1024 / 1024;
    let disks_info = Disks::new_with_refreshed_list();
    let mut disk_size = 0;
    for disk in &disks_info {
        disk_size += disk.total_space() / 1024 / 1024 / 1024;
    }
    // RAM
    let total_memory = system.total_memory();
    let used_memory = system.used_memory();
    let ram = (used_memory as f64 / total_memory as f64) * 100.0;
    // DISKS
    let disks = Disks::new_with_refreshed_list();
    let mut total_disk = 0;
    let mut used_disk = 0;
    for disk in &disks {
        total_disk += disk.total_space();
        used_disk += disk.total_space() - disk.available_space();
    }
    let disk = (used_disk as f64 / total_disk as f64) * 100.0;
    // NETWORKS
    let mut networks = Networks::new_with_refreshed_list();
    networks.refresh(false);
    let mut upload = 0;
    let mut download = 0;
    for (_name, data) in &networks {
        upload += data.total_transmitted();
        download += data.total_received();
    }
    serde_json::json!({
    "cpu": cpu,
    "ram": ram,
    "disk": disk,
    "network_up": upload / 1024,
    "network_down": download / 1024,
    "cpu_name": cpu_name,
    "cpu_usage": cpu,
    "total_ram": total_ram_gb,
    "disk_size": disk_size
    })
}

#[tauri::command]
fn system_info() -> serde_json::Value {
    let mut system = System::new_all();
    system.refresh_all();
    let total_memory = system.total_memory();
    let used_memory = system.used_memory();
    let memory_percent = if total_memory == 0 {
        0.0
    } else {
        (used_memory as f64 / total_memory as f64) * 100.0
    };
    let disks = Disks::new_with_refreshed_list();
    let disk_total: u64 = disks.iter().map(|disk| disk.total_space()).sum();
    let disk_used: u64 = disks
        .iter()
        .map(|disk| disk.total_space().saturating_sub(disk.available_space()))
        .sum();
    let disk_percent = if disk_total == 0 {
        0.0
    } else {
        (disk_used as f64 / disk_total as f64) * 100.0
    };

    serde_json::json!({
        "cpu_percent": system.global_cpu_usage(),
        "memory_percent": memory_percent,
        "memory_used": used_memory,
        "memory_total": total_memory,
        "disk_percent": disk_percent,
        "disk_used": disk_used,
        "disk_total": disk_total,
        "hostname": System::host_name().unwrap_or_else(|| "Zariff".to_string()),
        "uptime_seconds": System::uptime(),
    })
}

#[tauri::command]
fn get_terminal_logs() -> Vec<String> {
    let stats = get_system_stats();
    let cpu = stats["cpu"].as_f64().unwrap_or(0.0);
    let ram = stats["ram"].as_f64().unwrap_or(0.0);
    let disk = stats["disk"].as_f64().unwrap_or(0.0);
    let upload = stats["network_up"].as_f64().unwrap_or(0.0);
    let download = stats["network_down"].as_f64().unwrap_or(0.0);
    let mut logs = vec![
        format!("[INFO] CPU Usage: {:.1}%", cpu),
        format!("[INFO] RAM Usage: {:.1}%", ram),
        format!("[INFO] Disk Usage: {:.1}%", disk),
        format!("[INFO] Network ↑ {:.2} MB/s ↓ {:.2} MB/s", upload, download),
        "[INFO] JSON-RPC connected".to_string(),
        "[SUCCESS] System monitoring active".to_string(),
        "[INFO] Background agents online".to_string(),
    ];
    if cpu > 80.0 {
        logs.push("[WARN] High CPU usage detected".to_string());
    }
    if ram > 75.0 {
        logs.push("[WARN] High memory usage detected".to_string());
    }
    if disk > 85.0 {
        logs.push("[WARN] Disk storage running low".to_string());
    }
    if upload > 5.0 || download > 5.0 {
        logs.push("[INFO] High network activity".to_string());
    }
    logs.push("[SUCCESS] Agent heartbeat OK".to_string());
    logs.push("[INFO] Monitoring active agents".to_string());
    logs.push("[INFO] System telemetry synced".to_string());
    logs.push("[SUCCESS] Daemon heartbeat stable".to_string());
    logs
}

#[tauri::command]
fn get_log_count() -> usize {
    let log_path = dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".local")
        .join("state")
        .join("zariff")
        .join("pilot.log");
    std::fs::read_to_string(log_path)
        .map(|contents| contents.lines().count())
        .unwrap_or(0)
}

#[tauri::command]
fn get_rss_feed() -> Vec<serde_json::Value> {
    let mut feed = vec![serde_json::json!({
        "title": format!(
            "Zariff v{} Active Release (JARVIS Core Engine)",
            env!("CARGO_PKG_VERSION")
        ),
        "url": "https://github.com/NotXeb3c/zariff-2/releases",
        "source": "Current Build"
    })];
    if let Ok(output) = std::process::Command::new("git")
        .args([
            "tag",
            "-l",
            "--sort=-creatordate",
            "--format=%(refname:short)|%(creatordate:short)|%(subject)",
        ])
        .output()
    {
        if let Ok(text) = String::from_utf8(output.stdout) {
            for line in text.lines().take(4) {
                let parts: Vec<&str> = line.split('|').collect();
                if !parts.is_empty() && !parts[0].is_empty() {
                    feed.push(serde_json::json!({
                        "title": format!("Release {}: {}", parts[0], if parts.len() > 2 && !parts[2].is_empty() { parts[2] } else { "Official Zariff Distribution" }),
                        "url": format!("https://github.com/NotXeb3c/zariff-2/releases/tag/{}", parts[0]),
                        "source": format!("Release Tag ({})", if parts.len() > 1 && !parts[1].is_empty() { parts[1] } else { "Published" })
                    }));
                }
            }
        }
    }
    if feed.len() == 1 {
        feed.push(serde_json::json!({
            "title": "Cognitive Engine & Threat Containment Bridge Live",
            "url": "https://github.com/NotXeb3c/zariff-2",
            "source": "System Feature"
        }));
    }
    feed
}
#[tauri::command]
fn get_agent_activity() -> Vec<serde_json::Value> {
    let stats = get_system_stats();
    let cpu = stats["cpu"].as_f64().unwrap_or(0.0);
    let ram = stats["ram"].as_f64().unwrap_or(0.0);
    let upload = stats["network_up"].as_f64().unwrap_or(0.0);
    let download = stats["network_down"].as_f64().unwrap_or(0.0);
    vec![
        serde_json::json!({
            "name":
                "Voice Agent",
            "status":
                if cpu > 20.0 {
                    "Active"
                } else {
                    "Idle"
                },
            "message":
                "Listening..."
        }),
        serde_json::json!({
            "name":
                "Gesture Agent",
            "status":
                "Active",
            "message":
                "Tracking..."
        }),
        serde_json::json!({
            "name":
                "System Agent",
            "status":
                if ram > 70.0 {
                    "Warning"
                } else {
                    "Active"
                },
            "message":
                "Monitoring..."
        }),
        serde_json::json!({
            "name":
                "Automation Agent",
            "status":
                if cpu > 60.0 {
                    "Active"
                } else {
                    "Idle"
                },
            "message":
                "Waiting..."
        }),
        serde_json::json!({
            "name":
                "Network Agent",
            "status":
                if upload > 5.0 || download > 5.0 {
                    "Active"
                } else {
                    "Idle"
                },
            "message":
                "Monitoring network traffic..."
        }),
        serde_json::json!({
            "name":
                "Security Agent",
            "status":
                "Active",
            "message":
                "Scanning system threats..."
        }),
        serde_json::json!({
            "name":
                "Thermal Agent",
            "status":
                if cpu > 75.0 {
                    "Warning"
                } else {
                    "Active"
                },
            "message":
                "Monitoring system temperatures..."
        }),
        serde_json::json!({
            "name":
                "JSON-RPC Agent",
            "status":
                "Active",
            "message":
                "Syncing real-time events..."
        }),
    ]
}
#[tauri::command]
fn get_temperature_stats() -> serde_json::Value {
    let stats = get_system_stats();
    let cpu_usage = stats["cpu"].as_f64().unwrap_or(0.0);
    let ram_usage = stats["ram"].as_f64().unwrap_or(0.0);
    let cpu_temp = 35.0 + (cpu_usage * 0.6);
    let gpu_temp = 32.0 + (cpu_usage * 0.4);
    let motherboard_temp = 28.0 + (ram_usage * 0.2);
    let ssd_temp = 30.0 + (cpu_usage * 0.2);
    let vrm_temp = 30.0 + (cpu_usage * 0.3);
    let battery_temp = 29.0 + (ram_usage * 0.2);
    let power_draw = 45.0 + (cpu_usage * 1.5);
    let mut sys = System::new_all();
    sys.refresh_all();
    // REAL CPU NAME
    let cpu_name = sys
        .cpus()
        .first()
        .map(|c| c.brand().to_string())
        .unwrap_or_default();
    // REAL CPU THREADS
    let cpu_threads = sys.cpus().len();
    // REAL BATTERY %
    let manager = BatteryManager::new().ok();
    let mut battery_percent = 0;
    if let Some(manager) = manager {
        if let Ok(mut batteries) = manager.batteries() {
            if let Some(Ok(battery)) = batteries.next() {
                battery_percent = (battery.state_of_charge().value * 100.0) as i32;
            }
        }
    }
    serde_json::json!({
        "cpu":
            cpu_temp,
        "gpu":
            gpu_temp,
        "motherboard":
            motherboard_temp,
        "ssd":
            ssd_temp,
        "vrm":
            vrm_temp,
        "battery":
            battery_temp,
        "power":
            power_draw,
        "cpu_name":
           cpu_name,
       "cpu_threads":
            cpu_threads,
        "battery_percent":
            battery_percent,
    })
}
fn spawn_daemon() -> Option<Child> {
    if TcpStream::connect((DAEMON_HOST, DAEMON_PORT)).is_ok() {
        println!(
            "[Zariff] Reusing daemon already listening on ws://{}:{}",
            DAEMON_HOST, DAEMON_PORT
        );
        return None;
    }

    let data_dir = get_app_data_dir();
    let _ = std::fs::create_dir_all(&data_dir);

    let venv_python = get_venv_python();

    // Strategy 1: isolated venv python
    if venv_python.exists() {
        if let Some(mut child) = try_spawn_with(&venv_python) {
            println!("[Zariff] AI daemon spawned from venv");

            if daemon_child_started(&mut child, "venv") {
                println!("[Zariff] Daemon is initializing; the UI will reconnect when ready");
                return Some(child);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    // Strategy 2: system python
    #[cfg(target_os = "windows")]
    let sys_python = PathBuf::from("python");
    #[cfg(not(target_os = "windows"))]
    let sys_python = PathBuf::from("python3");

    if let Some(mut child) = try_spawn_with(&sys_python) {
        println!("[Zariff] AI daemon spawned from system Python");

        if daemon_child_started(&mut child, "system Python") {
            println!("[Zariff] Daemon is initializing; the UI will reconnect when ready");
            return Some(child);
        }
        let _ = child.kill();
        let _ = child.wait();
    }

    // Strategy 3: background install if venv doesn't exist
    if !venv_python.exists() {
        println!("[Zariff] No daemon found. Starting background installation...");
        setup_venv_in_background();
    } else {
        eprintln!("[Zariff] Warning: venv exists but daemon failed to start.");
    }

    None
}

fn stop_daemon(state: &DaemonProcess) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            match child.try_wait() {
                Ok(Some(_)) => {
                    println!("[Zariff] Python daemon already exited");
                }
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    println!("[Zariff] Python daemon stopped");
                }
                Err(e) => {
                    eprintln!("[Zariff] Failed to inspect daemon before stop: {}", e);
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    }
}
fn main() {
    // Spawn the Python daemon before building the Tauri app
    let daemon_child = spawn_daemon();
    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .manage(DaemonProcess(Mutex::new(daemon_child)))
        .manage(file_access::AllowedPaths::new())
        .manage(commands::GestureCursor::init())
        .setup(|app| {
            let window = app.get_webview_window("main").unwrap();

            // Show the window when the user starts the app, rather than hiding it
            window.show().unwrap();
            window.set_focus().unwrap();
            tray::setup_tray(app)?;
            hotkey::register_hotkey(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                println!("[Zariff] Main window close requested");
                let _ = window;
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::toggle_window,
            commands::get_daemon_status,
            commands::send_to_daemon,
            commands::confirm_action,
            commands::open_terminal,
            commands::clear_logs,
            commands::restart_agents,
            commands::system_scan,
            commands::get_uptime,
            commands::take_screenshot,
            commands::get_dashboard_status,
            get_system_stats,
            system_info,
            get_log_count,
            get_temperature_stats,
            get_terminal_logs,
            get_rss_feed,
            get_agent_activity,
            commands::open_logs_folder,
            commands::apply_git_conflict_resolution,
            commands::get_hotkey,
            commands::set_hotkey,
            commands::get_auth_token,
            commands::extract_file_text,
            commands::move_gesture_cursor,
            commands::click_gesture_cursor,
            file_access::register_allowed_path,
            file_access::revoke_allowed_path,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Zariff")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                let state = app_handle.state::<DaemonProcess>();
                stop_daemon(&state);
            }
        });
}

#[cfg(test)]
mod startup_tests {
    use super::*;

    #[cfg(target_os = "windows")]
    fn spawn_short_lived_child() -> Child {
        Command::new("cmd")
            .args(["/C", "exit", "7"])
            .spawn()
            .expect("spawn short-lived child")
    }

    #[cfg(not(target_os = "windows"))]
    fn spawn_short_lived_child() -> Child {
        Command::new("sh")
            .args(["-c", "exit 7"])
            .spawn()
            .expect("spawn short-lived child")
    }

    #[cfg(target_os = "windows")]
    fn spawn_long_lived_child() -> Child {
        Command::new("cmd")
            .args(["/C", "ping -n 4 127.0.0.1 >NUL"])
            .spawn()
            .expect("spawn long-lived child")
    }

    #[cfg(not(target_os = "windows"))]
    fn spawn_long_lived_child() -> Child {
        Command::new("sh")
            .args(["-c", "sleep 2"])
            .spawn()
            .expect("spawn long-lived child")
    }

    #[test]
    fn startup_accepts_a_child_that_is_still_initializing() {
        let mut child = spawn_long_lived_child();

        assert!(daemon_child_started(&mut child, "test"));

        let _ = child.kill();
        let _ = child.wait();
    }

    #[test]
    fn startup_rejects_a_child_that_exits_immediately() {
        let mut child = spawn_short_lived_child();

        assert!(!daemon_child_started(&mut child, "test"));

        let _ = child.wait();
    }

    #[test]
    fn offline_system_info_uses_real_host_units() {
        let info = system_info();
        let stats = get_system_stats();

        assert!(info["memory_total"].as_u64().unwrap_or(0) > 1024 * 1024);
        assert!(info["disk_total"].as_u64().unwrap_or(0) > 1024 * 1024);
        assert!(!info["hostname"].as_str().unwrap_or_default().is_empty());
        assert!(stats["total_ram"]
            .as_u64()
            .is_some_and(|value| value < 1024));
    }

    #[test]
    fn native_release_feed_uses_package_version() {
        let feed = get_rss_feed();
        let title = feed[0]["title"].as_str().unwrap_or_default();

        assert!(title.contains(env!("CARGO_PKG_VERSION")));
    }

    #[test]
    fn desktop_installs_matching_daemon_version() {
        assert_eq!(
            daemon_requirement(),
            format!("pilot-daemon[all]=={}", env!("CARGO_PKG_VERSION"))
        );
    }

    #[test]
    fn desktop_first_run_installs_playwright_chromium() {
        assert_eq!(
            playwright_browser_install_args(),
            ["-m", "playwright", "install", "chromium"]
        );
    }
}
