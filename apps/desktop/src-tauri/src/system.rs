//! Live machine metrics.
//!
//! Every field is an `Option`, so "the OS did not tell us" is representable and
//! reaches the UI as an em-dash rather than a zero. The frontend never
//! substitutes a placeholder number, so neither does this.

use std::sync::Mutex;

use serde::Serialize;
use sysinfo::{Disks, Networks, System};

/// Persistent sampler.
///
/// CPU percentage is a delta between two refreshes, so the `System` has to
/// survive between calls. The UI polls every 2s — comfortably above sysinfo's
/// minimum refresh interval — so the readings are real, not a first-sample
/// artefact.
pub struct Sampler {
    system: Mutex<System>,
    networks: Mutex<Networks>,
    disks: Mutex<Disks>,
    /// True once a second refresh has happened and CPU% is meaningful.
    warm: Mutex<bool>,
}

impl Sampler {
    pub fn new() -> Self {
        Self {
            system: Mutex::new(System::new_all()),
            networks: Mutex::new(Networks::new_with_refreshed_list()),
            disks: Mutex::new(Disks::new_with_refreshed_list()),
            warm: Mutex::new(false),
        }
    }
}

impl Default for Sampler {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
pub struct SystemSnapshot {
    cpu_percent: Option<f32>,
    mem_used_bytes: Option<u64>,
    mem_total_bytes: Option<u64>,
    disk_used_bytes: Option<u64>,
    disk_total_bytes: Option<u64>,
    net_rx_bytes: Option<u64>,
    net_tx_bytes: Option<u64>,
    battery_percent: Option<f32>,
    battery_charging: Option<bool>,
    process_count: Option<usize>,
    hostname: Option<String>,
    os_name: Option<String>,
    uptime_seconds: Option<u64>,
    captured_at: u64,
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Battery state, or `None` on a desktop with no battery or when the platform
/// refuses to report. A read failure is logged, not silently turned into 0%.
fn read_battery() -> (Option<f32>, Option<bool>) {
    let manager = match starship_battery::Manager::new() {
        Ok(m) => m,
        Err(e) => {
            log::debug!("battery manager unavailable: {e}");
            return (None, None);
        }
    };
    let mut batteries = match manager.batteries() {
        Ok(b) => b,
        Err(e) => {
            log::debug!("battery enumeration failed: {e}");
            return (None, None);
        }
    };
    match batteries.next() {
        Some(Ok(b)) => {
            let pct = b.state_of_charge().value * 100.0;
            let charging = matches!(
                b.state(),
                starship_battery::State::Charging | starship_battery::State::Full
            );
            (Some(pct), Some(charging))
        }
        // No battery present: a desktop, not an error.
        None => (None, None),
        Some(Err(e)) => {
            log::debug!("battery read failed: {e}");
            (None, None)
        }
    }
}

impl Sampler {
    /// Take a reading.
    ///
    /// Split out from the Tauri command so it can be unit-tested without a
    /// running app — the point being to prove these numbers come from the OS.
    pub fn snapshot(&self) -> Result<SystemSnapshot, String> {
        let mut sys = self.system.lock().map_err(|e| e.to_string())?;
        sys.refresh_cpu_usage();
        sys.refresh_memory();
        sys.refresh_processes(sysinfo::ProcessesToUpdate::All, true);

        let mut warm = self.warm.lock().map_err(|e| e.to_string())?;
        let cpu_percent = if *warm {
            Some(sys.global_cpu_usage())
        } else {
            // The first sample after construction has no previous tick to diff
            // against, so report "unknown" rather than a misleading 0%.
            *warm = true;
            None
        };

        let mem_total = sys.total_memory();
        let mem_used = sys.used_memory();
        let process_count = sys.processes().len();

        let mut disks = self.disks.lock().map_err(|e| e.to_string())?;
        disks.refresh();
        // Aggregate across mounted volumes. On Windows this is every fixed
        // volume; the per-drive breakdown lands with diagnostics in Phase 5.
        let (disk_total, disk_avail) = disks.iter().fold((0u64, 0u64), |(t, a), d| {
            (t + d.total_space(), a + d.available_space())
        });

        let mut networks = self.networks.lock().map_err(|e| e.to_string())?;
        networks.refresh();
        let (rx, tx) = networks.iter().fold((0u64, 0u64), |(r, t), (_, data)| {
            (r + data.total_received(), t + data.total_transmitted())
        });

        let (battery_percent, battery_charging) = read_battery();

        Ok(SystemSnapshot {
            cpu_percent,
            mem_used_bytes: Some(mem_used),
            mem_total_bytes: (mem_total > 0).then_some(mem_total),
            disk_used_bytes: (disk_total > 0).then(|| disk_total.saturating_sub(disk_avail)),
            disk_total_bytes: (disk_total > 0).then_some(disk_total),
            net_rx_bytes: Some(rx),
            net_tx_bytes: Some(tx),
            battery_percent,
            battery_charging,
            process_count: Some(process_count),
            hostname: System::host_name(),
            os_name: System::long_os_version().or_else(System::name),
            uptime_seconds: Some(System::uptime()),
            captured_at: now_ms(),
        })
    }
}

#[tauri::command]
pub fn system_snapshot(sampler: tauri::State<'_, Sampler>) -> Result<SystemSnapshot, String> {
    sampler.snapshot()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// These assertions only hold if the values really come from the OS, which
    /// is the point: the UI must never be fed synthesised metrics.
    #[test]
    fn reports_real_memory_and_process_counts() {
        let s = Sampler::new().snapshot().expect("snapshot");
        let total = s.mem_total_bytes.expect("total memory");
        let used = s.mem_used_bytes.expect("used memory");
        assert!(
            total > 64 * 1024 * 1024,
            "implausible total memory: {total}"
        );
        assert!(used > 0 && used <= total, "used {used} vs total {total}");
        assert!(s.process_count.unwrap_or(0) > 1, "process count looks fake");
        assert!(s.uptime_seconds.is_some());
        assert!(s.captured_at > 0);
    }

    #[test]
    fn first_cpu_sample_is_unknown_not_zero() {
        let sampler = Sampler::new();
        // No previous tick to diff against yet.
        assert!(sampler.snapshot().unwrap().cpu_percent.is_none());
    }

    #[test]
    fn cpu_percent_becomes_available_and_in_range() {
        let sampler = Sampler::new();
        let _ = sampler.snapshot().unwrap();
        std::thread::sleep(sysinfo::MINIMUM_CPU_UPDATE_INTERVAL);
        let cpu = sampler
            .snapshot()
            .unwrap()
            .cpu_percent
            .expect("cpu after warm-up");
        assert!((0.0..=100.0).contains(&cpu), "cpu out of range: {cpu}");
    }

    #[test]
    fn network_counters_are_monotonic() {
        let sampler = Sampler::new();
        let a = sampler.snapshot().unwrap().net_rx_bytes.unwrap();
        let b = sampler.snapshot().unwrap().net_rx_bytes.unwrap();
        assert!(b >= a, "rx counter went backwards: {a} -> {b}");
    }

    /// A host with no readable volumes must yield `None`, not `0`.
    #[test]
    fn disk_fields_are_consistent() {
        let s = Sampler::new().snapshot().unwrap();
        match (s.disk_total_bytes, s.disk_used_bytes) {
            (Some(total), Some(used)) => assert!(used <= total),
            (None, None) => {}
            other => panic!("inconsistent disk fields: {other:?}"),
        }
    }

    /// Not an assertion — prints a live reading so `cargo test -- --nocapture`
    /// shows that the panel is backed by real data.
    #[test]
    fn print_live_snapshot() {
        let sampler = Sampler::new();
        let _ = sampler.snapshot();
        std::thread::sleep(sysinfo::MINIMUM_CPU_UPDATE_INTERVAL);
        println!(
            "{}",
            serde_json::to_string_pretty(&sampler.snapshot().unwrap()).unwrap()
        );
    }

    #[test]
    fn serialises_to_camel_case_for_the_ui() {
        let s = Sampler::new().snapshot().unwrap();
        let json = serde_json::to_string(&s).unwrap();
        for key in [
            "cpuPercent",
            "memUsedBytes",
            "memTotalBytes",
            "diskUsedBytes",
            "diskTotalBytes",
            "netRxBytes",
            "netTxBytes",
            "batteryPercent",
            "batteryCharging",
            "processCount",
            "hostname",
            "osName",
            "uptimeSeconds",
            "capturedAt",
        ] {
            assert!(json.contains(key), "missing key {key} in {json}");
        }
    }
}
