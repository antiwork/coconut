"""Wake the reads that wait for new events when the writer commits.

A phone waits in GET /v1/updates for the next event. The wait read the event cursor every 0.1 s, so each update
reached the phone about 0.05 s late, and up to 0.1 s. The writer now tells the waiting reads at each commit. A wait
still ends after its 0.1 s, so a commit that tells nobody is found as before.
"""
import threading


class CommitSignal:
    def __init__(self):
        self.condition = threading.Condition()
        self.count = 0

    def notify(self):
        """Count one commit and wake every waiting read. The end of the service calls it too."""
        with self.condition:
            self.count += 1
            self.condition.notify_all()

    def wait(self, seen, seconds):
        """Wait for a commit after commit number `seen`, at most `seconds`. Return the number of the last commit."""
        with self.condition:
            if self.count == seen:
                self.condition.wait(seconds)
            return self.count
