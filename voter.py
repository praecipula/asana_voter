#!/usr/bin/env python3
import os
import yaml
import json
import logging
import logging.config
import argparse
import asana
import itertools


with open('logging.yaml') as fobj:
    logging.config.dictConfig(yaml.load(fobj))


logger = logging.getLogger("voter")
client = asana.Client.access_token(os.environ["ASANA_PERSONAL_ACCESS_TOKEN"])

class AsanaObj:
    def __init__(self):
        self._json = None

    def _get_json(self):
        if not self._json:
            self._json = self._asana_request()
            logger.debug(self._json)
        return self._json

class SourceTask(AsanaObj):
    def __init__(self, task_id):
        self._task_id = task_id
        super(SourceTask, self).__init__()

    def _asana_request(self):
        return client.tasks.find_by_id(self._task_id, fields=['name'])

    @property
    def id(self):
        return self._task_id

    @property
    def name(self):
        return self._get_json()['name']

    def __str__(self):
        return f"Task {self.name} ({self._task_id})"

class MetaStorage(AsanaObj):
    """Persistence class that stores data in Asana's external field
    in a particular task. Both stores YAML info in the external field (for this
    script to consume) and in the task description (for humans to consume)."""

    @classmethod
    def pop_from_project_tasks(klass, tasks, project_id):
        """ Given a set of tasks from the source project,
        pop the one that is the meta task.

        This is how we find the meta task on read starting from the project.
        """
        # The task that is this task, but is not recognized yet (the Python task to pop).
        meta_task = None
        for non_cast_task in tasks[:]:
            """Iterate over copy"""
            meta_task = MetaStorage(non_cast_task.id, project_id)
            if meta_task.is_meta_task():
                """Found it. Remove and return."""
                tasks.remove(non_cast_task)
                break
        return meta_task
     
    @classmethod
    def create(klass, project_id):
        bootstrap_external_info = {"version": "1.0"}
        ei_string = yaml.dump(bootstrap_external_info)
        metatask = client.tasks.create({"name": "Meta task (storing persistent info)", 
            "projects": [project_id]
            })
        ms = MetaStorage(metatask['id'], project_id)
        ms.external = bootstrap_external_info
        return ms

    def __init__(self, task_id, project_id):
        super(MetaStorage, self).__init__()
        self._task_id = task_id
        self._project_id = project_id

    def is_meta_task(self):
        return self.external != None

    def external_data_key(self):
        return f"python_metadata_{self._project_id}"

    @property
    def external(self):
        data = self._get_json()
        if 'external' not in data:
            return None
        if data['external']['id'] != self.external_data_key():
            return None
        return yaml.load(data['external']['data'])

    @external.setter
    def external(self, data):
        """Immediately sets the external data"""
        y = yaml.dump(data)
        client.tasks.update(self._task_id, {"notes": y,
            "external": {
                "id": self.external_data_key(),
                "data": y
                }
            })

    @property
    def version(self):
        if self.external == None:
            return None
        return self.external['version']

    def _asana_request(self):
        logger.debug(f"external:python_vote_metadata_{self._project_id}")
        return client.tasks.find_by_id(self._task_id, fields=['name', 'external'])



class SourceProject(AsanaObj):
    def __init__(self, project_id):
        self._project_id = project_id
        super(SourceProject, self).__init__()

    def _asana_request(self):
        return client.projects.find_by_id(self._project_id, fields=['name'])

    def tasks(self):
        task_generator = client.tasks.find_by_project(self._project_id, fields=['name', 'followers'])
        return[SourceTask(task_json['id']) for task_json in task_generator]
    
    @property
    def id(self):
        return self._project_id

    @property
    def name(self):
        return self._get_json()['name']

    def __str__(self):
        return f"Project {self.name} ({self._project_id})"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Make projects for comparison counted voting.")
    parser.add_argument("-p", "--project", help="The source project. Each task will be compared with each other.")
    args = parser.parse_args()
    logger.debug(args)

    if 'project' in args:
        p = SourceProject(args.project)
        ts = p.tasks()
        mt = MetaStorage.pop_from_project_tasks(ts, args.project)
        if mt:
            logger.info(f"Found meta task: {mt}")
        if not mt:
            logger.info("Creating new metastorage task")
            mt = MetaStorage.create(p.id)
        logger.info(mt)
        logger.info(mt.version)
        for t in ts:
            logger.info(t)
        logger.info([p for p in itertools.combinations([t.id for t in ts], 2)])
