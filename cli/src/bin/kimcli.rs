//! Standalone `kimcli` binary entry point.
//!
//! Identical to `kim tui` (see `kim_cli::commands::tui`) — this is just a
//! separate, directly-invokable binary target for the same launcher, so a
//! user (or `scripts/install_kimcli.sh`) can put `kimcli` on PATH and run it
//! without going through `kim tui`.

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let code = kim_cli::commands::tui::run_tui_standalone(args).await;
    std::process::exit(code);
}
