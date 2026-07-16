use std::io::{stdout, IsTerminal};

use crossterm::style::{Color as TerminalColor, Stylize};

use crate::app::{MessageRole, UiMessage};

fn colors_enabled() -> bool {
    stdout().is_terminal() && std::env::var_os("NO_COLOR").is_none()
}

pub(crate) fn kim_accent_color() -> TerminalColor {
    TerminalColor::Rgb {
        r: 0xe8,
        g: 0xb8,
        b: 0x9a,
    }
}

fn kim_dim_color() -> TerminalColor {
    TerminalColor::Grey
}

pub(crate) fn paint_text(text: &str) -> String {
    text.to_string()
}

pub(crate) fn paint_dim(text: &str) -> String {
    paint(text, kim_dim_color())
}

pub(crate) fn paint_bold(text: &str, color: TerminalColor) -> String {
    if colors_enabled() {
        format!("{}", text.with(color).bold())
    } else {
        text.to_string()
    }
}

fn paint(text: &str, color: TerminalColor) -> String {
    if colors_enabled() {
        format!("{}", text.with(color))
    } else {
        text.to_string()
    }
}

pub(crate) fn print_message(message: &UiMessage) {
    let label = match message.role {
        MessageRole::User => "You",
        MessageRole::Assistant => "Kim",
        MessageRole::System => "Note",
        MessageRole::Error => "Error",
        MessageRole::Reasoning => "Thinking",
    };
    for (index, line) in message.content.lines().enumerate() {
        if index == 0 {
            println!(
                "{} {}",
                paint_bold(&format!("{label}:"), kim_accent_color()),
                paint_text(line)
            );
        } else {
            println!("{}  {}", " ".repeat(label.len()), paint_text(line));
        }
    }
    if message.content.lines().next().is_none() {
        println!("{}", paint_bold(&format!("{label}:"), kim_accent_color()));
    }
}

pub(crate) fn print_note(message: &str) {
    println!("{}", paint_dim(message));
}
