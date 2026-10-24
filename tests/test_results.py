import tempfile
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from hltv_stats import results


class ResultsTests(unittest.TestCase):
    def setUp(self):
        results.START_TIME = time.time()
        self.state = {key: {} for key in
                      ('known_match_ids', 'enriched_match_ids', 'failed_match_ids')}

    def test_dates_preserve_months_and_calendar_day(self):
        for text, expected in [('August 21st 2026', '2026-08-21'),
                               ('October 23rd 2026', '2026-10-23')]:
            self.assertEqual(results.parse_result_date('Results for ' + text), expected)

    def test_duplicate_rows_keep_enrichment(self):
        rows = [{'match-id': 1, 'team1': 'new', 'date': None},
                {'match-id': 1, 'team1': 'old', 'date': '2026-08-21', 'maps': ['Nuke']}]
        self.assertEqual(results.deduplicate(rows), [
            {'match-id': 1, 'team1': 'new', 'date': '2026-08-21', 'maps': ['Nuke']}])

    def test_duplicate_does_not_replace_zero_score(self):
        rows = [{'match-id': 1, 'team1score': 0}, {'match-id': 1, 'team1score': 2}]
        self.assertEqual(results.deduplicate(rows)[0]['team1score'], 0)

    def test_legacy_failure_log_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'failed_urls.json'
            path.write_text('https://example.com\nhttps://example.com\n')
            with patch.object(results, 'FAILED_URLS_FILE', path):
                self.assertEqual(results.load_failed_urls(), ['https://example.com'])

    def test_known_integer_id_and_new_match_in_same_section(self):
        self.state['known_match_ids']['123'] = True
        page = BeautifulSoup('''<div class="results-holder">
            <span class="standard-headline">Results for August 21st 2026</span>
            <div class="result-con"><a class="a-reset" href="/matches/123/known"></a></div>
            <div class="result-con"><a class="a-reset" href="/matches/124/new"></a></div>
            <div class="result-con"><a class="a-reset" href="/matches/124/new"></a></div>
            </div>''', 'lxml')
        with patch.object(results, 'get_parsed_page', return_value=page) as fetch:
            rows = results.scrape_latest_results(self.state)
        self.assertEqual([r['match-id'] for r in rows], [124])
        fetch.assert_called_once()

    def test_failed_or_blocked_listing_is_an_error(self):
        for page in [None, BeautifulSoup('<html>Challenge</html>', 'lxml')]:
            with patch.object(results, 'get_parsed_page', return_value=page):
                with self.assertRaises(RuntimeError):
                    results.scrape_latest_results(self.state)

    def test_veto_does_not_overwrite_format(self):
        soup = BeautifulSoup('''<div class="col-6 col-7-small">
            <div class="standard-box veto-box">Best of 3<br>* Final</div>
            <div class="standard-box veto-box">1. A removed Nuke</div></div>''', 'lxml')
        parsed = results.parse_match_details(soup)
        self.assertEqual(parsed['format'], 'Best of 3')
        self.assertEqual(parsed['stage'], 'Final')
        self.assertEqual(parsed['veto'], ['1. A removed Nuke'])

    def test_blocked_match_is_not_marked_enriched(self):
        with patch.object(results, 'get_parsed_page', return_value=BeautifulSoup('Blocked', 'lxml')):
            results.enrich_matches([{'match-id': 1, 'url': 'https://example.com'}], self.state)
        self.assertNotIn('1', self.state['enriched_match_ids'])
        self.assertIn('1', self.state['failed_match_ids'])

    def test_recovered_match_clears_failure(self):
        self.state['failed_match_ids']['1'] = True
        soup = BeautifulSoup('''<div class="col-6 col-7-small">
            <div class="standard-box veto-box">Best of 3</div></div>''', 'lxml')
        with patch.object(results, 'get_parsed_page', return_value=soup):
            results.enrich_matches([{'match-id': 1, 'url': 'https://example.com'}], self.state)
        self.assertNotIn('1', self.state['failed_match_ids'])
        self.assertIn('1', self.state['enriched_match_ids'])

    def test_main_persists_deduplication_and_reconciles_stale_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {'match-id': 1, 'url': 'https://example.com', 'maps': []}
            (root / 'results.json').write_text(json.dumps([row, row]))
            self.state['known_match_ids']['2'] = True
            self.state['failed_match_ids']['1'] = True
            (root / 'scrape_state.json').write_text(json.dumps(self.state))
            with patch.multiple(results, RESULTS_FILE=root / 'results.json',
                                STATE_FILE=root / 'scrape_state.json',
                                FAILED_URLS_FILE=root / 'failed_urls.json'), \
                    patch.object(results, 'scrape_latest_results', return_value=[]), \
                    patch.dict('os.environ', {'GITHUB_STEP_SUMMARY': ''}):
                results.main()
            self.assertEqual(json.loads((root / 'results.json').read_text()), [row])
            state = json.loads((root / 'scrape_state.json').read_text())
            self.assertEqual(state['known_match_ids'], {'1': True})
            self.assertEqual(state['failed_match_ids'], {})

    def test_atomic_json_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            results.write_json(path, {'ok': True})
            self.assertEqual(path.read_text(), '{\n  "ok": true\n}')
            self.assertFalse(path.with_suffix('.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
