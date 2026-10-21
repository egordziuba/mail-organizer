from mailorganizer.gmail_sync import GmailSync
from mailorganizer.archive import Archive
from mailorganizer.connectors import ConnectorError


class Reader:
    def __init__(self,ids):
        self.ids=ids;self.history={"historyId":"final"};self.labels=["INBOX"];self.metadata_calls=[];self.raw_calls=[];self.fail=False
    def get_profile(self):return {"emailAddress":"synthetic@example.test","historyId":"baseline"}
    def list_page(self,**kwargs):return {"messages":[{"id":mid} for mid in self.ids]}
    def history_page(self,*args,**kwargs):return self.history
    def get_raw(self,mid):
        self.raw_calls.append(mid)
        return {"id":mid,"raw":b"Subject: SYNTHETIC\r\n\r\nBody","labelIds":self.labels,"historyId":"fresh"}
    def get_metadata(self,mid):
        self.metadata_calls.append(mid)
        if self.fail:raise ConnectorError("synthetic",http_status=503)
        return {"id":mid,"labelIds":self.labels,"historyId":"fresh"}


def test_failed_metadata_retries_without_claiming_completion(tmp_path):
    Archive(tmp_path).ingest("gmail-1","pilot",b"Subject: SYNTHETIC\r\n\r\nBody")
    reader=Reader(["pilot"]);reader.fail=True
    sync=GmailSync(tmp_path,"gmail-1",reader,min_free_bytes=0)
    assert sync.step()["failed"] == 1
    reader.fail=False
    with sync._connect() as db:db.execute("UPDATE queue SET retry_at=0")
    result=sync.step()
    assert reader.metadata_calls == ["pilot","pilot"]
    assert not reader.raw_calls
    assert result["full_mailbox_verified"]


def test_current_get_is_not_overwritten_by_old_history_snapshot(tmp_path):
    reader=Reader(["new"]);reader.labels=["STARRED"]
    reader.history={"historyId":"old","history":[{"messagesAdded":[{"message":{"id":"new","labelIds":["INBOX"],"historyId":"old"}}]}]}
    sync=GmailSync(tmp_path,"gmail-1",reader,min_free_bytes=0)
    sync.step()
    assert sync.message_metadata()["new"]["labelIds"] == ["STARRED"]
    assert sync.message_metadata()["new"]["historyId"] == "fresh"


def test_expired_cursor_rescan_refreshes_surviving_original_metadata(tmp_path):
    reader=Reader(["kept"]);sync=GmailSync(tmp_path,"gmail-1",reader,min_free_bytes=0);sync.step()
    reader.labels=["STARRED"];original=reader.history_page
    def expired(*a,**k):
        reader.history_page=original
        raise ConnectorError("expired",http_status=404)
    reader.history_page=expired
    sync.step(max_pages=1)
    assert sync.step()["full_mailbox_verified"]
    assert reader.metadata_calls == ["kept"]
    assert reader.raw_calls == ["kept"]
    assert sync.message_metadata()["kept"]["labelIds"] == ["STARRED"]
