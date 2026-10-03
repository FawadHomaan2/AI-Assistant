// Release builds on Windows must not open a console window behind the UI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    juno_lib::run()
}
