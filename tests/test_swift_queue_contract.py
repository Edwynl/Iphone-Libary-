"""Execute the actual Swift SQL on local SQLite; this is NOT a Swift/iOS build."""
import re
import sqlite3
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'ios/PhotosCompanion/Sources/AssetSyncQueue.swift'
SQL = {name: value for name, value in re.findall(r'static let (\w+) = """(.*?)"""', SOURCE.read_text(), re.S)}


def database():
    db = sqlite3.connect(':memory:')
    db.executescript(SQL['schema'])
    return db


def discover(db, assets, *, receiver='r', generation='g', now=1000, finish=True):
    db.executemany(SQL['discoverSQL'], [(receiver, ident, version, changed, now, generation)
                                       for ident, version, changed in assets])
    if finish:
        db.execute('INSERT OR REPLACE INTO queue_snapshot VALUES(?,?)', (receiver, generation))


def pending(db, receiver='r', limit=20):
    return db.execute(SQL['pendingSQL'], (receiver, receiver, limit)).fetchall()


def test_new_old_dated_import_precedes_unfinished_backlog_and_completed_stays_complete():
    db = database()
    old = [('old' + str(i), 'v1', 900 - i) for i in range(200)]
    discover(db, old)
    db.execute(SQL['completeSQL'], (1100, 'r', 'old0', 'v1'))
    discover(db, list(reversed(old)) + [('new-import-old-exif', 'v1', 1)], generation='g2', now=1200)
    assert pending(db)[0][0] == 'new-import-old-exif'
    assert db.execute('SELECT state FROM asset_queue WHERE asset=?', ('old0',)).fetchone()[0] == 'complete'
    # Stable IDs survive deletion/reordering; no numeric fetch position is persisted.
    discover(db, [old[170], old[40]], generation='g3', now=1300)
    assert {row[0] for row in pending(db)} == {'old170', 'old40'}


def test_changed_version_resets_state_but_stale_result_cannot_complete_it():
    db = database()
    discover(db, [('a', 'v1', 1), ('b', 'v1', 5)])
    db.execute(SQL['completeSQL'], (1100, 'r', 'a', 'v1'))
    discover(db, [('a', 'v2', 1), ('b', 'v1', 5)], generation='g2', now=1200)
    db.execute(SQL['completeSQL'], (1300, 'r', 'a', 'v1'))
    assert pending(db)[0][:2] == ('a', 'v2')


def test_receivers_snapshots_retry_backoff_and_audit_are_independent():
    db = database()
    discover(db, [('a', 'v1', 1), ('b', 'v1', 2)])
    discover(db, [('a', 'v1', 1)], receiver='other')
    db.execute(SQL['failureSQL'], (1100, 'r', 'a', 'v1'))
    assert db.execute(SQL['retrySQL'], ('r', 'r', 1159)).fetchall() == []
    assert db.execute(SQL['retrySQL'], ('r', 'r', 1160)).fetchone()[0] == 'a'
    db.execute(SQL['failureSQL'], (1160, 'r', 'a', 'v1'))
    assert db.execute('SELECT attempts,retry_after FROM asset_queue WHERE receiver=? AND asset=?', ('r', 'a')).fetchone() == (2, 1280)
    assert pending(db, 'other')[0][0] == 'a'
    db.execute(SQL['completeSQL'], (1000, 'r', 'b', 'v1'))
    assert db.execute(SQL['auditSQL'], ('r', 'r', 1000)).fetchall() == []
    assert db.execute(SQL['auditSQL'], ('r', 'r', 1001)).fetchone()[0] == 'b'
    # A cancelled partial discovery must not activate undiscovered/new jobs.
    discover(db, [('new', 'v1', 3)], generation='partial', now=1300, finish=False)
    assert not pending(db)
    discover(db, [('b', 'v1', 2), ('new', 'v1', 3)], generation='next', now=1400)
    assert pending(db)[0][0] == 'new'
    assert db.execute(SQL['remainingSQL'], ('r', 'r')).fetchone()[0] == 1


def test_100000_metadata_entries_new_import_has_priority(record_property):
    db = database()
    assets = [('asset-' + str(i), 'v1', 100000 - i) for i in range(100000)]
    started = time.perf_counter()
    discover(db, assets)
    discover(db, assets + [('fresh-with-old-date', 'v1', 0)], generation='g2', now=2000)
    assert pending(db, limit=18)[0][0] == 'fresh-with-old-date'
    record_property('sqlite_metadata_seconds', round(time.perf_counter() - started, 3))
    assert db.execute(SQL['remainingSQL'], ('r', 'r')).fetchone()[0] == 100001


def test_small_successful_batch_is_not_a_complete_library_snapshot():
    db = database()
    assets = [('asset-' + str(i), 'v1', i) for i in range(100)]
    discover(db, assets, generation='full-selection')
    for ident, version, _ in assets[:20]:
        db.execute(SQL['completeSQL'], (1100, 'r', ident, version))
    assert db.execute(SQL['remainingSQL'], ('r', 'r')).fetchone()[0] == 80
    assert db.execute(SQL['snapshotSQL'], ('r',)).fetchone()[0] == 'full-selection'
    assert db.execute(SQL['snapshotSQL'], ('another-receiver',)).fetchone() is None
    before = db.total_changes
    pending(db)
    assert db.total_changes == before  # Queue reads do not rediscover/reset the snapshot.
    for ident, version, _ in assets[20:]:
        db.execute(SQL['completeSQL'], (1200, 'r', ident, version))
    assert db.execute(SQL['remainingSQL'], ('r', 'r')).fetchone()[0] == 0


def test_incomplete_discovery_does_not_provide_a_completed_generation():
    db = database()
    discover(db, [('a', 'v1', 1)], generation='interrupted', finish=False)
    assert db.execute(SQL['snapshotSQL'], ('r',)).fetchone() is None
    discover(db, [('a', 'v1', 1), ('b', 'v1', 2)], generation='retry')
    assert db.execute(SQL['snapshotSQL'], ('r',)).fetchone()[0] == 'retry'
    assert db.execute(SQL['remainingSQL'], ('r', 'r')).fetchone()[0] == 2
