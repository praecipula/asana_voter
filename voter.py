#!/usr/bin/env python3
import os
import re
import yaml
import json
import logging
import logging.config
import argparse
import asana
import itertools
import random


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

class AsanaProject(AsanaObj):
    def project_link(self):
        return f"https://app.asana.com/0/{self._obj_id}/list"

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
        self._user_task_list_url = None

    def _asana_request(self):
        return client.users.find_by_id(self._obj_id, fields=['name'])

    def __str__(self):
        return f"User {self.name} ({self.id})"

    def task_list_user_link_url(self, obj_has_workspace_id):
        # TODO: this will someday be a full client library method;
        # Here it's just an (approved) workaround until the client libraries get updated.
        if not self._user_task_list_url:
            task_list = client.get(f"/users/{self.id}/user_task_list", {"workspace": obj_has_workspace_id._get_json()['workspace']['id']})
            # For some reason, LunaText parses the final single quote as part of the url, so insert extra space.
            self._user_task_list_url = f"https://app.asana.com/0/{task_list['id']}/list "
        return self._user_task_list_url
        

class SourceProject(AsanaProject):
    def __init__(self, project_id):
        self._source_tasks = None
        self._meta_task = None
        super(SourceProject, self).__init__(project_id)

    def _asana_request(self):
        return client.projects.find_by_id(self._obj_id, fields=['name', 'followers', 'team', 'workspace'])

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
        # TODO: this has to be called after meta task popped :/ remove this implicit dependency.
        if not self._source_tasks:
            task_generator = client.tasks.find_by_project(self._obj_id, fields=['name'])
            self._source_tasks = [SourceTask(task_json['id']) for task_json in task_generator]
        return self._source_tasks

    
    def combinations(self):
        # Ensure metatask is popped; see TODO in source project#tasks
        mt = self.meta_task()
        """Generate all combos of tasks"""
        combos = [p for p in itertools.combinations([str(t.id) for t in self.tasks], 2)]
        logger.info(combos)
        return combos

    @property
    def voters(self):
        return [Voter(follower_json['id']) for follower_json in self._get_json()['followers']]

    @property
    def team_id(self):
        return self._get_json()['team']['id']
    
    def __str__(self):
        return f"Project {self.name} ({self.id})"

class DestTask(AsanaObj):

    EXT_ID_REGEX = re.compile(r"""voting_task_
    (?P<dest_project_id>\d+)_
    (?P<first_task_id>\d+)_
    (?P<second_task_id>\d+)""", re.X)

    @classmethod
    def create(klass, dest_project, source_task_one, source_task_two):
        # Sort lexicographically by source task index
        if source_task_one.id == source_task_two.id:
            raise "Task IDs must be different"
        ordered_tasks = [source_task_one, source_task_two]
        ordered_tasks.sort(key=lambda t: t.id)
        random_tasks = ordered_tasks[:]
        random.shuffle(random_tasks)
        logger.info(f"Creating new dest task from {source_task_one} and {source_task_two}")
        dest_task = client.tasks.create({"name": f"Vote: {random_tasks[0].name} vs {random_tasks[1].name}",
            "external": {
                "id": f"voting_task_{dest_project.id}_{ordered_tasks[0].id}_{ordered_tasks[1].id}"
                },
            "projects": [dest_project.id]
            })
        return DestTask(dest_task['id'], source_task_one, source_task_two)

    def __init__(self, task_id, source_task_one = None, source_task_two = None):
        super(DestTask, self).__init__(task_id)
        # If given, these are set
        # If not, they can be looked up from the external field.
        self._source_task_one = source_task_one
        self._source_task_two = source_task_two

    def _asana_request(self):
        return client.tasks.find_by_id(self._obj_id, fields=['name', 'external'])

    @property
    def first_source_task(self):
        if not self._source_task_one:
            logger.info(f"Looking up source task one from external ID")
            data = self._get_json()
            if 'external' not in data:
                return None
            task_data = DestTask.EXT_ID_REGEX.match(data['external']['id'])
            self._source_task_one = SourceTask(task_data['first_task_id'])
        return self._source_task_one

    @property
    def second_source_task(self):
        if not self._source_task_two:
            logger.info(f"Looking up source task two from external ID")
            data = self._get_json()
            if 'external' not in data:
                return None
            task_data = DestTask.EXT_ID_REGEX.match(data['external']['id'])
            self._source_task_two = SourceTask(task_data['second_task_id'])
        return self._source_task_two




class DestProject(AsanaProject):
    @classmethod
    def create(klass, source_project, voter):
        project = client.projects.create({"name": f"Voting project from {source_project.name}", 
            "owner": voter.id,
            "team": source_project.team_id,
            "notes": "Voter project for {voter.name}; original project {source_project.project_link}"
            })
        return DestProject(project['id'], voter)

    def __init__(self, project_id, voter):
        super(DestProject, self).__init__(project_id)
        self._voter = voter
        self._voting_task_index = None
        self._dest_tasks = None

    def _asana_request(self):
        return client.projects.find_by_id(self._obj_id, fields=['name', 'followers', 'team', 'workspace'])

    @property
    def voter(self):
        return self._voter

    def current_voting_tasks(self):
        """Find (memoized) all the current voting tasks.
        Also denormalizes into a map of combinations.
        """
        if not self._dest_tasks:
            self._voting_task_index = {}
            task_generator = client.tasks.find_by_project(self._obj_id, fields=['name'])
            self._dest_tasks = [DestTask(task_json['id']) for task_json in task_generator]
            for dtask in self._dest_tasks:
                if dtask.first_source_task.id not in self._voting_task_index:
                    self._voting_task_index[dtask.first_source_task.id] = {}
                if dtask.second_source_task.id not in self._voting_task_index[dtask.first_source_task.id]:
                    self._voting_task_index[dtask.first_source_task.id][dtask.second_source_task.id] = dtask
        return self._dest_tasks


    def find_voting_task_by_source_tasks(self, source_task_one, source_task_two):
        """Search the voting tasks for the one with the indices given, or None if does not exist."""
        # Ensure the index is built
        self.current_voting_tasks()
        ordered_tasks = [source_task_one, source_task_two]
        ordered_tasks.sort(key=lambda t: t.id)
        if ordered_tasks[0].id not in self._voting_task_index:
            return None
        if ordered_tasks[1].id not in self._voting_task_index[ordered_tasks[0].id]:
            return None
        return self._voting_task_index[ordered_tasks[0].id][ordered_tasks[1].id]

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
        logger.info(f"Source project: {p}")
        logger.info(f"Voters: {[str(v) for v in p.voters]}")


    if 'generate' in args:
        if 'voter_projects' not in mt.external:
            logger.info("Creating voter projects record in meta")
            mt.external['voter_projects'] = {}
            mt.flush()
        dest_projects = []
        for v in p.voters:
            if v.id not in mt.external['voter_projects']:
                logger.info(f"Creating voter project for {v}")
                dest = DestProject.create(p, v)
                project_data = {
                        "id": dest.id,
                        "voter_link": v.task_list_user_link_url(dest),
                        "project_link": dest.project_link()
                        }
                mt.external['voter_projects'][v.id] = project_data
                mt.flush()
            else:
                dest = DestProject(mt.external['voter_projects'][v.id]['id'], v)
            dest_projects.append(dest)
            logger.info(f"Voting project for {v}: {dest}")

        logger.info(f"Projects {[str(d) for d in dest_projects]}")
        source_project_task_combos = p.combinations()
        for dest_proj in dest_projects:
            for combo in source_project_task_combos:
                dest_task = dest_proj.find_voting_task_by_source_tasks(SourceTask(combo[0]), SourceTask(combo[1]))
                if not dest_task:
                    dest_task = DestTask.create(dest_proj, SourceTask(combo[0]), SourceTask(combo[1]))

