import unittest

from timeoff.contracts import DomainError
from timeoff.holiday_presets import us_federal_holidays


class FederalHolidayPresetTests(unittest.TestCase):
    def test_2025_preset_returns_named_observed_federal_holidays(self):
        holidays = us_federal_holidays(2025)
        self.assertEqual(len(holidays), 11)
        self.assertEqual(holidays[0], {"date": "2025-01-01", "name": "New Year's Day"})
        self.assertIn({"date": "2025-02-17", "name": "Washington's Birthday (Presidents' Day)"}, holidays)
        self.assertIn({"date": "2025-06-19", "name": "Juneteenth National Independence Day"}, holidays)
        self.assertIn({"date": "2025-11-27", "name": "Thanksgiving Day"}, holidays)
        self.assertEqual(len({item["date"] for item in holidays}), len(holidays))

    def test_weekend_holiday_moves_to_the_federal_observed_weekday(self):
        holidays = us_federal_holidays(2026)
        self.assertIn({"date": "2026-07-03", "name": "Independence Day (observed)"}, holidays)
        self.assertNotIn({"date": "2026-07-04", "name": "Independence Day"}, holidays)

    def test_year_preset_includes_new_year_observed_on_prior_december_31(self):
        holidays = us_federal_holidays(2021)
        self.assertIn({"date": "2021-12-31", "name": "New Year's Day (observed)"}, holidays)

    def test_year_must_be_an_integer_in_supported_range(self):
        for year in (True, "2025", 1899, 2101):
            with self.subTest(year=year), self.assertRaises(DomainError):
                us_federal_holidays(year)


if __name__ == "__main__":
    unittest.main()
