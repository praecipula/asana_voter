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
    def __init__(self, obj_id):
        self._obj_id = obj_id
        self._json = None

    @property
    def id(self):
        return self._obj_id

    @property
    def name(self):
        return self._get_json()['name']

    def _get_json(self):
        if not self._json:
            self._json = self._asana_request()
        return self._json

class SourceTask(AsanaObj):
    def __init__(self, task_id):
        super(SourceTask, self).__init__(task_id)

    def _asana_request(self):
        return client.tasks.find_by_id(self._obj_id, fields=['name'])

    def __str__(self):
        return f"Task {self.name} ({self.id})"

class MetaStorage(AsanaObj):
    """Persistence class that stores data in Asana's external field
    in a particular task. Both stores YAML info in the external field (for this
    script to consume) and in the task description (for humans to consume)."""

     
    @classmethod
    def create(klass, project_id):
        bootstrap_external_info = {"version": "1.0"}
        metatask = client.tasks.create({"name": "Meta task (storing persistent info)", 
            "projects": [project_id]
            })
        ms = MetaStorage(metatask['id'], project_id)
        ms.external = bootstrap_external_info
        ms.flush()
        return ms

    def __init__(self, task_id, project_id):
        super(MetaStorage, self).__init__(task_id)
        self._project_id = project_id
        self._data = None

    def is_meta_task(self):
        return self.external != None

    def external_data_key(self):
        return f"python_metadata_{self._project_id}"

    @property
    def external(self):
        if not self._data:
            data = self._get_json()
            if 'external' not in data:
                return None
            if data['external']['id'] != self.external_data_key():
                return None
            self._data = yaml.load(data['external']['data'])
        return self._data


    @external.setter
    def external(self, data):
        """ Set the "local" data. You may want to call flush() after this to write to Asana."""
        self._data = data

    def flush(self):
        y = yaml.dump(self._data)
        client.tasks.update(self._obj_id, {"notes": y,
            "external": {
                "id": self.external_data_key(),
                "data": y
                }
            })
        self._json = None
        self._data = None

    @property
    def version(self):
        if self.external == None:
            return None
        return self.external['version']

    def _asana_request(self):
        return client.tasks.find_by_id(self._obj_id, fields=['name', 'external'])

    def __str__(self):
        return f"MetaStorage: ({self.id})\n{yaml.dump(self.external)}"

class Voter(AsanaObj):
    def __init__(self, user_id):
        super(Voter, self).__init__(user_id)

    def _asana_request(self):
        return client.users.find_by_id(self._obj_id, fields=['name'])

    def __str__(self):
        return f"User {self.name} ({self.id})"

class SourceProject(AsanaObj):
    def __init__(self, project_id):
        self._source_tasks = None
        self._meta_task = None
        super(SourceProject, self).__init__(project_id)

    def _asana_request(self):
        return client.projects.find_by_id(self._obj_id, fields=['name', 'followers', 'team'])

    def __str__(self):
        return f"Source project {self.name} ({self.id})"

    def meta_task(self):
        """Find or create the meta task on the project"""
        # The task that is this task, but is not recognized yet (the Python task to pop).
        if self._meta_task is None:
            local_meta_task = None
            for non_cast_task in self.tasks[:]:
                """Iterate over copy"""
                local_meta_task = MetaStorage(non_cast_task.id, self.id)
                if local_meta_task.is_meta_task():
                    """Found it. Remove and return."""
                    self.tasks.remove(non_cast_task)
                    self._meta_task = local_meta_task
                    break
            if self._meta_task:
                logger.debug(f"Found meta task: {self._meta_task}")
            if not self._meta_task:
                logger.debug("Creating new metastorage task")
                self._meta_task = MetaStorage.create(p.id)
        return self._meta_task

    @property
    def tasks(self):
        """Get the project task as SourceTasks. Memoized."""
        """TODO: If/when we run this "live" we should listen for events; probably just blow away
        cache at that point and do a rescan, since the size of the project should remain small.
        Or else people will be voting forever."""
        if not self._source_tasks:
            task_generator = client.tasks.find_by_project(self._obj_id, fields=['name'])
            self._source_tasks = [SourceTask(task_json['id']) for task_json in task_generator]
        return self._source_tasks

    @property
    def voters(self):
        return [Voter(follower_json['id']) for follower_json in self._get_json()['followers']]

    @property
    def team_id(self):
        return self._get_json()['team']['id']
    
    def __str__(self):
        return f"Project {self.name} ({self.id})"


class DestProject(AsanaObj):
    @classmethod
    def create(klass, source_project, voter):
        project = client.projects.create({"name": f"Voting project from {source_project.name}", 
            "owner": voter.id,
            "team": source_project.team_id
            })
        return DestProject(project['id'], voter)

    def __init__(self, project_id, voter):
        super(DestProject, self).__init__(project_id)
        self._voter = voter

    def _asana_request(self):
        return client.projects.find_by_id(self._obj_id, fields=['name', 'followers'])

    @property
    def voter(self):
        return self._voter

    def __str__(self):
        return f"Dest project {self.name} ({self.id}) for {self.voter}"



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Make projects for comparison counted voting.")
    parser.add_argument("-p", "--project", help="The source project. Each task will be compared with each other.")
    parser.add_argument("-g", "--generate", help="Generate projects for each voter if needed", action='store_true')
    args = parser.parse_args()
    logger.debug(args)

    if 'project' in args:
        p = SourceProject(args.project)
        mt = p.meta_task()
        ts = p.tasks
        for t in ts:
            logger.info(t)
        logger.info([p for p in itertools.combinations([t.id for t in ts], 2)])
        for v in p.voters:
            logger.info(v)

    if 'generate' in args:
        logger.info(p)
        if 'voter_projects' not in mt.external:
            logger.info("Creating voter projects record in meta")
            mt.external['voter_projects'] = {}
            mt.flush()
        for v in p.voters:
            if v.id not in mt.external['voter_projects']:
                logger.info(f"Creating voter project for {v}")
                dest = DestProject.create(p, v)
                mt.external['voter_projects'][v.id] = dest.id
                mt.flush()
            else:
                dest = DestProject(mt.external['voter_projects'][v.id], v)
            logger.info(dest)

