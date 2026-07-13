//! F6/T1: single owned stdin reader for interactive approval prompts.
//!
//! The old pattern spawned a fresh `spawn_blocking(read_line)` per prompt.
//! Blocking threads can't be aborted: a Ctrl-C mid-approval left the thread
//! parked in `read(0)` forever, and every such leak raced rustyline for the
//! user's *next* typed line (which could then be applied as an answer to a
//! dead prompt). This module owns exactly one reader thread for the whole
//! process and correlates each read with its requester through a dedicated
//! oneshot channel:
//!
//! - at most one thread ever touches stdin on behalf of approvals;
//! - a request whose caller went away (Ctrl-C / turn abort) has a dead
//!   oneshot, so the line eventually read for it is **discarded** — a late
//!   answer is never applied to the next prompt;
//! - requests are served strictly in order, so two overlapping prompts can't
//!   swap answers.
//!
//! A line typed while a cancelled read is still pending is consumed and
//! dropped (that is unavoidable with blocking stdin), but it can no longer
//! approve anything.

use std::sync::mpsc as std_mpsc;
use std::sync::OnceLock;

use tokio::sync::oneshot;

struct Request {
    /// `None` payload = EOF or read error.
    reply: oneshot::Sender<Option<String>>,
}

/// Spawn the reader thread over an arbitrary line source (injectable for
/// tests). Returns the request queue handle.
fn spawn_reader<F>(mut read_line: F) -> std_mpsc::Sender<Request>
where
    F: FnMut() -> Option<String> + Send + 'static,
{
    let (tx, rx) = std_mpsc::channel::<Request>();
    std::thread::spawn(move || {
        while let Ok(req) = rx.recv() {
            let line = read_line();
            // If the requester cancelled (its oneshot receiver dropped), this
            // send fails and the line is DISCARDED — never delivered to a
            // later prompt.
            let _ = req.reply.send(line);
        }
    });
    tx
}

static READER: OnceLock<std_mpsc::Sender<Request>> = OnceLock::new();

/// Read one line from stdin through the process-wide owned reader.
/// Returns `None` on EOF or read error. Cancellation-safe: dropping the
/// returned future discards (rather than re-routes) the pending line.
pub(crate) async fn read_stdin_line() -> Option<String> {
    let sender = READER.get_or_init(|| {
        spawn_reader(|| {
            let mut line = String::new();
            match std::io::stdin().read_line(&mut line) {
                Ok(0) | Err(_) => None,
                Ok(_) => Some(line),
            }
        })
    });
    let (reply_tx, reply_rx) = oneshot::channel();
    sender.send(Request { reply: reply_tx }).ok()?;
    reply_rx.await.ok().flatten()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The T1 guarantee: a line arriving for a cancelled request is discarded,
    /// and the NEXT request gets the next fresh line — never the stale one.
    #[tokio::test]
    async fn cancelled_request_line_is_discarded_not_applied_to_next() {
        let (line_tx, line_rx) = std_mpsc::channel::<String>();
        let sender = spawn_reader(move || line_rx.recv().ok());

        // Request A is issued and immediately cancelled (simulates Ctrl-C
        // during an approval prompt).
        let (a_tx, a_rx) = oneshot::channel();
        sender.send(Request { reply: a_tx }).unwrap();
        drop(a_rx);

        // Request B queues behind A (the next approval prompt).
        let (b_tx, b_rx) = oneshot::channel();
        sender.send(Request { reply: b_tx }).unwrap();

        // Two lines arrive: the first answers the dead request A and must be
        // discarded; the second must reach B.
        line_tx.send("stale-yes".to_string()).unwrap();
        line_tx.send("n".to_string()).unwrap();

        let got = b_rx.await.expect("B must receive a reply");
        assert_eq!(
            got.as_deref(),
            Some("n"),
            "B must get the fresh line, never the stale answer meant for A"
        );
    }

    /// Requests are answered strictly in order.
    #[tokio::test]
    async fn requests_are_served_in_order() {
        let (line_tx, line_rx) = std_mpsc::channel::<String>();
        let sender = spawn_reader(move || line_rx.recv().ok());

        let (a_tx, a_rx) = oneshot::channel();
        sender.send(Request { reply: a_tx }).unwrap();
        let (b_tx, b_rx) = oneshot::channel();
        sender.send(Request { reply: b_tx }).unwrap();

        line_tx.send("first".to_string()).unwrap();
        line_tx.send("second".to_string()).unwrap();

        assert_eq!(a_rx.await.unwrap().as_deref(), Some("first"));
        assert_eq!(b_rx.await.unwrap().as_deref(), Some("second"));
    }

    /// EOF / read failure surfaces as None.
    #[tokio::test]
    async fn eof_maps_to_none() {
        let (line_tx, line_rx) = std_mpsc::channel::<String>();
        drop(line_tx); // source is at EOF immediately
        let sender = spawn_reader(move || line_rx.recv().ok());
        let (a_tx, a_rx) = oneshot::channel();
        sender.send(Request { reply: a_tx }).unwrap();
        assert_eq!(a_rx.await.unwrap(), None);
    }
}
