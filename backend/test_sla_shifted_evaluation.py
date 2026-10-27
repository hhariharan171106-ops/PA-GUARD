import unittest

from evaluate_sla_shifted import SHIFT, SHIFTED_SEED, metrics, shifted_data


class ShiftedSlaEvaluationTests(unittest.TestCase):
    def test_shifted_generator_is_reproducible_and_separate(self):
        rows_a, labels_a = shifted_data(80, SHIFTED_SEED)
        rows_b, labels_b = shifted_data(80, SHIFTED_SEED)
        self.assertEqual(rows_a, rows_b)
        self.assertEqual(labels_a, labels_b)
        self.assertNotEqual(SHIFT["duration_noise_sigma"], .55)
        self.assertNotEqual(SHIFT["hours_range"], [1., 240.])

    def test_metrics_include_false_alarm_and_calibration_fields(self):
        result = metrics([0, 0, 1, 1], [.1, .8, .7, .9], .5)
        self.assertEqual(result["confusion_matrix_actual_0_1_pred_0_1"], [[1, 1], [0, 2]])
        self.assertEqual(result["false_positive_rate"], .5)
        self.assertEqual(result["alerts_per_100"], 75.)
        self.assertEqual(result["false_alarm_fraction_of_alerts"], 1/3)
        self.assertIn("reliability_curve", result)


if __name__ == "__main__":
    unittest.main()
