use std::time::Duration;

use ratatui::{
    layout::{Alignment, Constraint, Direction, Layout, Margin, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
    Frame,
};

use crate::theme::Theme;

/* ===========================================================
color helpers
=========================================================== */

fn rgb_of(c: Color) -> (u8, u8, u8) {
    match c {
        Color::Rgb(r, g, b) => (r, g, b),
        _ => (200, 200, 200),
    }
}

pub fn lerp_color(a: Color, b: Color, mix: f32) -> Color {
    let (ar, ag, ab) = rgb_of(a);
    let (br, bg, bb) = rgb_of(b);
    let m = mix.clamp(0.0, 1.0);
    let r = (ar as f32 + (br as f32 - ar as f32) * m) as u8;
    let g = (ag as f32 + (bg as f32 - ag as f32) * m) as u8;
    let b = (ab as f32 + (bb as f32 - ab as f32) * m) as u8;
    Color::Rgb(r, g, b)
}

/* ===========================================================
spinners
=========================================================== */

#[derive(Clone, Copy, Debug)]
#[allow(dead_code)]
pub enum SpinnerStyle {
    Braille,
    Line,
    Moon,
    Block,
}

impl SpinnerStyle {
    fn frames(self) -> &'static [&'static str] {
        match self {
            SpinnerStyle::Braille => &["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
            SpinnerStyle::Line => &["—", "\\", "|", "/"],
            SpinnerStyle::Moon => &["◐", "◓", "◑", "◒"],
            SpinnerStyle::Block => &["▖", "▘", "▝", "▗"],
        }
    }
    fn period_ms(self) -> u64 {
        match self {
            SpinnerStyle::Braille => 80,
            SpinnerStyle::Line => 110,
            SpinnerStyle::Moon => 190,
            SpinnerStyle::Block => 150,
        }
    }
}

pub fn spinner_span(now: Duration, style: SpinnerStyle, color: Color) -> Span<'static> {
    let frames = style.frames();
    let idx = (now.as_millis() as u64 / style.period_ms()) as usize % frames.len();
    Span::styled(
        frames[idx].to_string(),
        Style::default().fg(color).add_modifier(Modifier::BOLD),
    )
}

/* ===========================================================
pulse dot
=========================================================== */

pub fn pulse_dot_span(now: Duration, accent: Color, dim: Color) -> Span<'static> {
    let phase = (now.as_millis() as f32 / 1400.0) * std::f32::consts::TAU;
    let mix = (phase.sin() * 0.5 + 0.5).powf(1.3);
    let color = lerp_color(dim, accent, mix);
    let glyph = if mix > 0.66 {
        "●"
    } else if mix > 0.22 {
        "•"
    } else {
        "·"
    };
    let mut style = Style::default().fg(color);
    if mix > 0.5 {
        style = style.add_modifier(Modifier::BOLD);
    }
    Span::styled(glyph.to_string(), style)
}

/* ===========================================================
shimmer
=========================================================== */

pub fn shimmer_line(text: &str, now: Duration, base: Color, accent: Color) -> Line<'static> {
    let chars: Vec<char> = text.chars().collect();
    let len = chars.len().max(1) as f32;
    let cycle_ms = 1700.0;
    let gap_ms = 600.0;
    let width = (len * 0.55).max(5.0);
    let t_in_cycle = (now.as_millis() as f32) % (cycle_ms + gap_ms);
    let progress = (t_in_cycle / cycle_ms).min(1.0);
    let center = progress * (len + width) - width / 2.0;
    let spans: Vec<Span<'static>> = chars
        .into_iter()
        .enumerate()
        .map(|(i, ch)| {
            let d = ((i as f32) - center).abs() / (width / 2.0);
            let mix = (1.0 - d).max(0.0).powf(1.8);
            let color = lerp_color(base, accent, mix);
            let mut style = Style::default().fg(color);
            if mix > 0.65 {
                style = style.add_modifier(Modifier::BOLD);
            }
            Span::styled(ch.to_string(), style)
        })
        .collect();
    Line::from(spans)
}

/* ===========================================================
blinking caret
=========================================================== */

pub fn cursor_span(now: Duration, color: Color) -> Span<'static> {
    if (now.as_millis() / 530) % 2 == 0 {
        Span::styled("▌", Style::default().fg(color).add_modifier(Modifier::BOLD))
    } else {
        Span::raw(" ")
    }
}

/* ===========================================================
trace items
=========================================================== */

#[derive(Clone, Debug)]
pub enum TraceItem {
    Thought(String),
    Tool { verb: String, target: String },
    Plan(Plan),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Pending,
    Active,
    Done,
}

#[derive(Clone, Debug)]
pub struct PlanItem {
    pub text: String,
    pub status: Status,
}

#[derive(Clone, Debug)]
pub struct Plan {
    pub title: String,
    pub items: Vec<PlanItem>,
}

fn verb_color(verb: &str) -> Color {
    match verb {
        "Reading" | "Read" => Color::Rgb(0xc8, 0x95, 0x44),
        "Running" | "Run" => Color::Rgb(0x6b, 0xa1, 0x82),
        "Writing" | "Write" => Color::Rgb(0x9a, 0x4f, 0x24),
        "Editing" | "Edit" => Color::Rgb(0xb2, 0x6a, 0x3c),
        "Searching" | "Find" => Color::Rgb(0x6b, 0x82, 0xa1),
        "Thinking" => Color::Rgb(0x8a, 0x76, 0x9a),
        _ => Color::Rgb(0x9a, 0x4f, 0x24),
    }
}

/* ===========================================================
scripted demo — kept for optional demo use
=========================================================== */

#[allow(dead_code)]
#[derive(Clone, Debug)]
pub struct Script {
    pub steps: Vec<(TraceItem, u64)>,
}

#[allow(dead_code)]
impl Script {
    pub fn visible(&self, now: Duration) -> Vec<&TraceItem> {
        let ms = now.as_millis() as u64;
        self.steps
            .iter()
            .filter(|(_, t0)| *t0 <= ms)
            .map(|(it, _)| it)
            .collect()
    }

    pub fn total_ms(&self) -> u64 {
        self.steps.last().map(|(_, t0)| t0 + 2500).unwrap_or(1000)
    }

    pub fn demo() -> Self {
        let plan = Plan {
            title: "Plan".into(),
            items: vec![
                PlanItem {
                    text: "Wire a CPU opponent into pong.html".into(),
                    status: Status::Done,
                },
                PlanItem {
                    text: "Add Easy / Medium / Hard difficulty".into(),
                    status: Status::Done,
                },
                PlanItem {
                    text: "Add local PvP toggle".into(),
                    status: Status::Done,
                },
                PlanItem {
                    text: "Build tower_sim.html with 5 balls".into(),
                    status: Status::Active,
                },
                PlanItem {
                    text: "Add rotating platforms + collision".into(),
                    status: Status::Pending,
                },
                PlanItem {
                    text: "Tune ball physics".into(),
                    status: Status::Pending,
                },
            ],
        };
        Script {
            steps: vec![
                (TraceItem::Thought("Two candidates — pong.html and index.html. Let me figure out which is canonical.".into()), 0),
                (TraceItem::Tool { verb: "Running".into(), target: "find . -maxdepth 2 -name '*.html'".into() }, 1100),
                (TraceItem::Tool { verb: "Reading".into(), target: "pong.html".into() }, 2200),
                (TraceItem::Tool { verb: "Reading".into(), target: "index.html".into() }, 3100),
                (TraceItem::Thought("pong.html is the polished one — index.html is the old prototype. Here's the plan:".into()), 4200),
                (TraceItem::Plan(plan), 5400),
                (TraceItem::Thought("Pong + PvP wired up cleanly. Onto the tower sim — 5 colored balls.".into()), 7000),
                (TraceItem::Tool { verb: "Writing".into(), target: "tower_sim.html".into() }, 8400),
                (TraceItem::Tool { verb: "Editing".into(), target: "styles.css".into() }, 9600),
            ],
        }
    }
}

/* ===========================================================
main panel renderer — takes a live slice + elapsed time
=========================================================== */

pub fn draw_thinking_panel(
    f: &mut Frame,
    area: Rect,
    items: &[TraceItem],
    elapsed: Duration,
    theme: Theme,
) {
    f.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.border))
        .style(Style::default().bg(theme.panel).fg(theme.text));
    let inner = block.inner(area);
    f.render_widget(block, area);

    let body = inner.inner(Margin {
        horizontal: 2,
        vertical: 1,
    });
    if body.height < 3 {
        return;
    }

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .split(body);

    draw_header(f, rows[0], elapsed, items.len(), theme);
    draw_rule(f, rows[1], theme);
    draw_stream(f, rows[3], items, elapsed, theme);
    draw_keybar(f, rows[5], theme);
}

fn draw_header(f: &mut Frame, area: Rect, elapsed: Duration, step_count: usize, theme: Theme) {
    let dot = pulse_dot_span(elapsed, theme.accent, theme.text_dimmest);
    let title = shimmer_line("Thinking", elapsed, theme.text_dim, theme.accent);
    let elapsed_s = (elapsed.as_millis() as u64 / 1000) % 999;

    let mut left = vec![dot, Span::raw("  ")];
    left.extend(title.spans);

    let right = Paragraph::new(Line::from(vec![
        Span::styled(
            format!("{}:{:02}", elapsed_s / 60, elapsed_s % 60),
            Style::default().fg(theme.text_dimmer),
        ),
        Span::styled("   ", Style::default()),
        Span::styled(
            format!(
                "{} step{}",
                step_count,
                if step_count == 1 { "" } else { "s" }
            ),
            Style::default().fg(theme.text_dimmest),
        ),
    ]))
    .alignment(Alignment::Right);

    f.render_widget(Paragraph::new(Line::from(left)), area);
    f.render_widget(right, area);
}

fn draw_rule(f: &mut Frame, area: Rect, theme: Theme) {
    let w = area.width as usize;
    let dashes: String = (0..w).map(|i| if i % 2 == 0 { '·' } else { ' ' }).collect();
    f.render_widget(
        Paragraph::new(Span::styled(
            dashes,
            Style::default().fg(theme.text_dimmest),
        )),
        area,
    );
}

fn draw_stream(f: &mut Frame, area: Rect, items: &[TraceItem], elapsed: Duration, theme: Theme) {
    if items.is_empty() {
        let line = Line::from(vec![
            spinner_span(elapsed, SpinnerStyle::Braille, theme.accent),
            Span::raw("  "),
            Span::styled(
                "Waiting for model stream",
                Style::default().fg(theme.text_dim),
            ),
        ]);
        f.render_widget(Paragraph::new(line), area);
        return;
    }
    let last_idx = items.len() - 1;
    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, it) in items.iter().enumerate() {
        let is_active = i == last_idx;
        match it {
            TraceItem::Thought(text) => lines.push(render_thought(text, is_active, elapsed, theme)),
            TraceItem::Tool { verb, target } => {
                lines.push(render_tool(verb, target, is_active, elapsed, theme));
            }
            TraceItem::Plan(plan) => {
                lines.extend(render_plan(plan, elapsed, theme));
            }
        }
    }
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn render_thought(text: &str, is_active: bool, elapsed: Duration, theme: Theme) -> Line<'static> {
    let marker = if is_active {
        cursor_span(elapsed, theme.accent)
    } else {
        Span::styled("›", Style::default().fg(theme.text_dimmest))
    };
    let mut spans = vec![marker, Span::raw("  ")];
    if is_active {
        spans.extend(shimmer_line(text, elapsed, theme.text_dim, theme.text).spans);
    } else {
        spans.push(Span::styled(
            text.to_string(),
            Style::default().fg(theme.text_dimmer),
        ));
    }
    Line::from(spans)
}

fn render_tool(
    verb: &str,
    target: &str,
    is_active: bool,
    elapsed: Duration,
    theme: Theme,
) -> Line<'static> {
    let marker = if is_active {
        cursor_span(elapsed, theme.accent)
    } else {
        Span::styled("›", Style::default().fg(theme.text_dimmest))
    };
    let vcol = verb_color(verb);
    let (verb_style, target_style) = if is_active {
        (
            Style::default().fg(vcol).add_modifier(Modifier::BOLD),
            Style::default().fg(theme.text),
        )
    } else {
        (
            Style::default().fg(lerp_color(vcol, theme.panel, 0.55)),
            Style::default().fg(theme.text_dimmer),
        )
    };
    let mut spans = vec![marker, Span::raw("  ")];
    if is_active {
        spans.push(spinner_span(elapsed, SpinnerStyle::Braille, vcol));
        spans.push(Span::raw(" "));
    } else {
        spans.push(Span::styled(
            "✓",
            Style::default()
                .fg(lerp_color(vcol, theme.panel, 0.4))
                .add_modifier(Modifier::BOLD),
        ));
        spans.push(Span::raw(" "));
    }
    spans.push(Span::styled(format!("{:<8}", verb), verb_style));
    spans.push(Span::raw(" "));
    spans.push(Span::styled(target.to_string(), target_style));
    Line::from(spans)
}

fn render_plan(plan: &Plan, elapsed: Duration, theme: Theme) -> Vec<Line<'static>> {
    let mut out: Vec<Line<'static>> = Vec::new();
    let done = plan
        .items
        .iter()
        .filter(|i| i.status == Status::Done)
        .count();
    let total = plan.items.len();
    out.push(Line::from(vec![
        Span::raw("    "),
        Span::styled("┌─ ", Style::default().fg(theme.accent)),
        Span::styled(
            plan.title.to_uppercase(),
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("  ", Style::default()),
        Span::styled(
            format!("{}/{}", done, total),
            Style::default().fg(theme.text_dimmer),
        ),
    ]));
    for (i, item) in plan.items.iter().enumerate() {
        let is_last = i == plan.items.len() - 1;
        let elbow = if is_last { "└" } else { "│" };
        let (box_glyph, fg, modifier) = match item.status {
            Status::Done => ("[✓]", theme.text_dim, Modifier::CROSSED_OUT),
            Status::Active => ("[●]", theme.accent, Modifier::BOLD),
            Status::Pending => ("[ ]", theme.text_dimmer, Modifier::empty()),
        };
        let mut spans = vec![
            Span::raw("    "),
            Span::styled(format!("{} ", elbow), Style::default().fg(theme.accent)),
            Span::styled(
                box_glyph.to_string(),
                Style::default().fg(fg).add_modifier(Modifier::BOLD),
            ),
            Span::raw("  "),
        ];
        if item.status == Status::Active {
            spans.extend(shimmer_line(&item.text, elapsed, theme.text_dim, theme.text).spans);
        } else {
            spans.push(Span::styled(
                item.text.clone(),
                Style::default().fg(fg).add_modifier(modifier),
            ));
        }
        out.push(Line::from(spans));
    }
    out
}

fn draw_keybar(f: &mut Frame, area: Rect, theme: Theme) {
    let para = Paragraph::new(Line::from(vec![
        Span::styled(
            " esc ",
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("cancel   ", Style::default().fg(theme.text_dimmer)),
    ]))
    .alignment(Alignment::Right);
    f.render_widget(para, area);
}

#[allow(dead_code)]
pub fn draw_spinner_strip(f: &mut Frame, area: Rect, now: Duration, theme: Theme) {
    let label_style = Style::default().fg(theme.text_dimmest);
    let mut spans: Vec<Span<'static>> = Vec::new();
    for (name, style) in [
        ("braille", SpinnerStyle::Braille),
        ("line", SpinnerStyle::Line),
        ("moon", SpinnerStyle::Moon),
        ("block", SpinnerStyle::Block),
    ] {
        spans.push(spinner_span(now, style, theme.accent));
        spans.push(Span::raw(" "));
        spans.push(Span::styled(name, label_style));
        spans.push(Span::raw("    "));
    }
    spans.push(pulse_dot_span(now, theme.accent, theme.text_dimmest));
    spans.push(Span::raw(" "));
    spans.push(Span::styled("pulse", label_style));
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}
