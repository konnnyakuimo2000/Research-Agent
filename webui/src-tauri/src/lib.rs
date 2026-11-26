// Research-Agent のデスクトップアプリの本体（.claude/webui-framework-and-app-plan.md 第 2 段）。
// 画面は webui/ をデスクトップ用にビルドしたもの（dist-desktop/）。調査はゲートウェイ（この PC の Docker か研究室の入口）がする。
// - 鍵: ゲートウェイの鍵は Windows の資格情報マネージャーに、つなぐ先ごとに置く（get_key / set_key。画面の core/api.ts が呼ぶ）
// - トレイ: 窓を閉じてもトレイに残り、開いている調査の購読と知らせを続ける。トレイの「終了」で終わる
// - 2 つ目を起動したら、すでに開いている窓を前に出す
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WindowEvent};

const SERVICE: &str = "Research-Agent";

fn entry(gateway: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, gateway).map_err(|e| e.to_string())
}

/// つなぐ先の鍵（なければ None）
#[tauri::command]
fn get_key(gateway: String) -> Result<Option<String>, String> {
    match entry(&gateway)?.get_password() {
        Ok(key) => Ok(Some(key)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

/// つなぐ先の鍵を置く（空なら消す）
#[tauri::command]
fn set_key(gateway: String, key: String) -> Result<(), String> {
    let e = entry(&gateway)?;
    if key.is_empty() {
        match e.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(err.to_string()),
        }
    } else {
        e.set_password(&key).map_err(|err| err.to_string())
    }
}

fn show(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show(app)))
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![get_key, set_key])
        .setup(|app| {
            let open = MenuItem::with_id(app, "open", "開く", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "終了", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().cloned().expect("アイコンがない"))
                .tooltip("Research-Agent")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show(tray.app_handle());
                    }
                })
                .build(app)?;
            Ok(())
        })
        // 窓を閉じても終わらず、トレイに残る（調査が答えを待つ・終わると知らせるため）
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("Research-Agent を起動できません");
}
