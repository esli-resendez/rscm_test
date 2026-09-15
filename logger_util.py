import os
from datetime import datetime

class Logger:
    def __init__(self, task_id, node_pos=4, serial=None):
        self.task_id = task_id
        self.node_pos = node_pos
        self.serial = serial
        self.timestamp = datetime.now().strftime("%y%m%d%H%M%S")
        self.filename = self._build_name()
        os.makedirs(os.path.dirname(self.filename), exist_ok=True)
        self.file = open(self.filename, "a", encoding="utf-8")

    def _build_name(self):
        sn = f"_sn_{self.serial}" if self.serial else ""
        return f"output/ssh_task_{self.task_id}_node_{self.node_pos}{sn}_{self.timestamp}.log"

    def rename(self, serial):
        """Re-point the log at a filename containing the unit serial."""
        if not serial or serial == self.serial:
            return self.filename
        self.serial = serial
        new_name = self._build_name()
        self.file.close()
        os.replace(self.filename, new_name)      # atomic, same directory
        self.filename = new_name
        self.file = open(self.filename, "a", encoding="utf-8")
        self.log("LOGGER", f"Log renamed to {self.filename}")
        return self.filename

    def log(self, command, output):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.file.write(f"[{ts}] COMMAND: {command}\n")
        self.file.write(f"[{ts}] OUTPUT:\n{output}\n")
        self.file.write("=" * 60 + "\n")
        self.file.flush()

    def close(self):
        if not self.file.closed:
            self.file.close()