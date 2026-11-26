"""Energia de la încărcător (energy_in) pentru încărcări.

Rulare (din rădăcina repository-ului, are nevoie doar de httpx, importat de analysis.py):
    python -m unittest discover -s tests
"""
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import analysis  # noqa: E402

VIN = "TESTVIN"
H = 3600


class ChargeEnergyTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            """
            CREATE TABLE telemetry_log (vin TEXT, ts REAL, field TEXT, value REAL);
            CREATE TABLE telemetry_events (vin TEXT, ts REAL, field TEXT, value TEXT);
            CREATE TABLE telemetry_loc (vin TEXT, ts REAL, lat REAL, lon REAL);
            """
        )
        analysis.init_db(self.con)
        self.t0 = time.time() - 2 * 86400

    def log(self, ts, field, value):
        self.con.execute("INSERT INTO telemetry_log VALUES (?,?,?,?)", (VIN, ts, field, value))

    def event(self, ts, value):
        self.con.execute("INSERT INTO telemetry_events VALUES (?,?,?,?)", (VIN, ts, "DetailedChargeState", value))

    def charges(self):
        analysis.analyze_charges(self.con, VIN, 0, time.time())
        return self.con.execute("SELECT * FROM charges WHERE vin=? ORDER BY start_ts", (VIN,)).fetchall()

    def ac_session_with_stale_dc(self):
        """Sesiune AC de 9,4 kWh (măsurată la wallbox); contorul DC a rămas la 27,4 kWh de la ultima
        încărcare rapidă, a raportat 0 o dată între timp și e retrimis neschimbat în timpul sesiunii."""
        t = self.t0
        self.log(t - 2 * H, "DCChargingEnergyIn", 0.0)
        self.event(t, "DetailedChargeStateCharging")
        for i in range(5):   # 4 h la ~2,35 kW
            ts = t + i * H
            self.log(ts, "ACChargingEnergyIn", 2.35 * i)
            self.log(ts, "DCChargingEnergyIn", 27.4)
            self.log(ts, "ACChargingPower", 2.4)
            self.log(ts, "EnergyRemaining", 40.0 + 2.1 * i)
            self.log(ts, "Soc", 55.0 + 2.8 * i)
        self.event(t + 4 * H, "DetailedChargeStateComplete")
        self.event(t + 5 * H, "DetailedChargeStateDisconnected")
        return t

    def test_ac_session_ignores_stale_dc_counter(self):
        self.ac_session_with_stale_dc()
        (c,) = self.charges()
        self.assertFalse(c["dc"])
        self.assertAlmostEqual(c["energy_in"], 9.4, places=2)
        d = analysis.charge_dict(self.con, c)
        self.assertEqual(d["charger_kwh"], 9.4)
        self.assertEqual(d["avg_kw"], 2.4)          # 9,4 kWh / 4 h de încărcare efectivă
        self.assertEqual(d["efficiency"], 89)       # 8,4 kWh în baterie / 9,4 kWh de la încărcător

    def test_ac_dc_split(self):
        self.ac_session_with_stale_dc()
        t = self.t0 + 10 * H
        self.event(t, "DetailedChargeStateCharging")
        for i in range(4):
            self.log(t + i * 600, "DCChargingEnergyIn", 10.0 * i)
            self.log(t + i * 600, "DCChargingPower", 120.0)
            self.log(t + i * 600, "EnergyRemaining", 20.0 + 9.5 * i)
        self.event(t + 1800, "DetailedChargeStateDisconnected")
        self.charges()
        s = analysis.charged_split(self.con, VIN)
        self.assertAlmostEqual(s["ac_kwh"], 9.4, places=2)
        self.assertAlmostEqual(s["dc_kwh"], 30.0, places=2)
        self.assertEqual(s["charges"], 2)
        self.assertEqual(s["since"], self.t0)

    def test_counter_reset_mid_session_counts_only_increases(self):
        t = self.t0
        self.event(t, "DetailedChargeStateCharging")
        for i, v in enumerate([12.0, 13.0, 14.0, 0.0, 1.5, 3.0]):   # resetare după 14,0
            self.log(t + i * 600, "ACChargingEnergyIn", v)
            self.log(t + i * 600, "EnergyRemaining", 30.0 + 0.9 * i)
        self.event(t + 3600, "DetailedChargeStateDisconnected")
        (c,) = self.charges()
        self.assertAlmostEqual(c["energy_in"], 5.0, places=2)   # 12→14 și 0→3

    def test_dc_session_uses_dc_counter(self):
        t = self.t0
        self.event(t, "DetailedChargeStateCharging")
        for i in range(4):
            self.log(t + i * 600, "DCChargingEnergyIn", 10.0 * i)
            self.log(t + i * 600, "ACChargingEnergyIn", 9.4)   # rămas de la ultima încărcare AC
            self.log(t + i * 600, "DCChargingPower", 120.0)
            self.log(t + i * 600, "EnergyRemaining", 20.0 + 9.5 * i)
        self.event(t + 1800, "DetailedChargeStateDisconnected")
        (c,) = self.charges()
        self.assertTrue(c["dc"])
        self.assertAlmostEqual(c["energy_in"], 30.0, places=2)

    def plugged_overnight(self, evening_kwh):
        """Cuplată pe 24.11 la 15:35, decuplată pe 25.11 la 12:50 (cazul real al încărcării 399)."""
        from datetime import datetime, timezone
        ts = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=timezone.utc).timestamp()
        self.event(ts(24, 15, 35), "DetailedChargeStateCharging")
        self.log(ts(24, 15, 35), "ACChargingEnergyIn", 0.0)
        self.log(ts(24, 15, 36), "ACChargingEnergyIn", evening_kwh)
        self.event(ts(24, 15, 36), "DetailedChargeStateStopped")
        self.event(ts(25, 4, 55), "DetailedChargeStateCharging")
        for i in range(8):   # 04:55 → 11:55, 0,6 kWh pe oră
            self.log(ts(25, 4, 55) + i * H, "ACChargingEnergyIn", evening_kwh + 0.6 * (i + 1))
        self.event(ts(25, 12, 37), "DetailedChargeStateStopped")
        self.event(ts(25, 12, 50), "DetailedChargeStateDisconnected")
        self.charges()
        return analysis.charge_entries(self.con, VIN, "2026-11-24", "2026-11-25", timezone.utc)

    def test_energy_counted_on_the_day_it_entered(self):
        entries = self.plugged_overnight(0.01)   # pe 24 doar urme de energie
        self.assertEqual([(e["day"], e["kwh"]) for e in entries], [("2026-11-25", 4.81)])
        self.assertEqual(entries[0]["start_day"], "2026-11-24")
        self.assertEqual(entries[0]["parts"], 1)

    def test_charge_over_two_days_has_two_entries(self):
        entries = self.plugged_overnight(1.5)
        self.assertEqual([(e["day"], e["kwh"]) for e in entries], [("2026-11-25", 4.8), ("2026-11-24", 1.5)])
        self.assertEqual([(e["part"], e["parts"], e["next_day"]) for e in entries],
                         [(2, 2, None), (1, 2, "2026-11-25")])
        self.assertEqual(entries[0]["day_charging_min"], 462)   # 04:55 → 12:37
        self.assertEqual(entries[1]["day_charging_min"], 1)

    def test_unchanged_counter_falls_back_to_battery_energy(self):
        t = self.t0
        self.event(t, "DetailedChargeStateCharging")
        for i in range(3):
            self.log(t + i * 600, "ACChargingEnergyIn", 9.4)   # nu se schimbă: ignorat
            self.log(t + i * 600, "EnergyRemaining", 50.0 + 1.0 * i)
        self.event(t + 1800, "DetailedChargeStateDisconnected")
        (c,) = self.charges()
        self.assertAlmostEqual(c["energy_in"], 2.0, places=2)


if __name__ == "__main__":
    unittest.main()
