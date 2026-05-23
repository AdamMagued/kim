use ratatui::style::Color;

use crate::config::ThemeName;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    pub name: ThemeName,
    pub bg: Color,
    pub panel: Color,
    pub panel_alt: Color,
    pub text: Color,
    pub text_dim: Color,
    pub text_dimmer: Color,
    pub text_dimmest: Color,
    pub border: Color,
    pub accent: Color,
    pub accent_ink: Color,
    pub accent_soft: Color,
    pub status: Color,
    pub status_alt: Color,
    pub danger: Color,
    pub success: Color,
}

impl Theme {
    pub const fn for_name(name: ThemeName) -> Self {
        match name {
            ThemeName::DarkNeovim => Self {
                name,
                bg: Color::Rgb(0x11, 0x10, 0x0f),
                panel: Color::Rgb(0x0c, 0x0b, 0x0a),
                panel_alt: Color::Rgb(0x1a, 0x16, 0x14),
                text: Color::Rgb(0xd4, 0xc8, 0xb8),
                text_dim: Color::Rgb(0xa8, 0x9d, 0x94),
                text_dimmer: Color::Rgb(0x7a, 0x6f, 0x60),
                text_dimmest: Color::Rgb(0x3f, 0x38, 0x30),
                border: Color::Rgb(0x2a, 0x23, 0x20),
                accent: Color::Rgb(0xe8, 0xb8, 0x9a),
                accent_ink: Color::Rgb(0x1a, 0x0e, 0x08),
                accent_soft: Color::Rgb(0x22, 0x1c, 0x19),
                status: Color::Rgb(0x1a, 0x16, 0x14),
                status_alt: Color::Rgb(0x2a, 0x23, 0x20),
                danger: Color::Rgb(0xef, 0x8b, 0x8b),
                success: Color::Rgb(0x9a, 0xd8, 0xa2),
            },
            ThemeName::QuietLight => Self {
                name,
                bg: Color::Rgb(0xe9, 0xe1, 0xd4),
                panel: Color::Rgb(0xde, 0xd4, 0xc6),
                panel_alt: Color::Rgb(0xd8, 0xcd, 0xbf),
                text: Color::Rgb(0x35, 0x30, 0x2a),
                text_dim: Color::Rgb(0x68, 0x5f, 0x55),
                text_dimmer: Color::Rgb(0x8d, 0x83, 0x75),
                text_dimmest: Color::Rgb(0xad, 0xa2, 0x92),
                border: Color::Rgb(0xc2, 0xb6, 0xa7),
                accent: Color::Rgb(0x8e, 0x62, 0x49),
                accent_ink: Color::Rgb(0xf2, 0xec, 0xe2),
                accent_soft: Color::Rgb(0xd2, 0xc3, 0xb3),
                status: Color::Rgb(0xd8, 0xcd, 0xbf),
                status_alt: Color::Rgb(0xcb, 0xbe, 0xae),
                danger: Color::Rgb(0x8f, 0x4d, 0x48),
                success: Color::Rgb(0x5d, 0x78, 0x55),
            },
        }
    }
}
