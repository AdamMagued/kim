use std::path::Path;

fn main() {
    tauri_build::build();

    // Bake the Kim project root at compile time so the production .app bundle
    // can find the Python interpreter and orchestrator modules even when the
    // executable is deep inside Contents/MacOS/ with no kim/ ancestor.
    //
    // CARGO_MANIFEST_DIR = .../kim/desktop/src-tauri
    // Project root = two parents up (.../kim/)
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let project_root = Path::new(manifest_dir)
        .parent() // .../kim/desktop
        .and_then(|p| p.parent()); // .../kim

    if let Some(root) = project_root {
        if root.join("orchestrator").join("agent.py").exists() {
            println!(
                "cargo:rustc-env=KIM_COMPILE_TIME_ROOT={}",
                root.display()
            );
        }
    }

    // Re-run this build script if agent.py changes (keeps the baked path fresh).
    println!("cargo:rerun-if-changed=../../orchestrator/agent.py");
    println!("cargo:rerun-if-changed=build.rs");
}
