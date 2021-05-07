import numpy as np
from abc import abstractmethod
from MotorNet.nets.losses import position_loss, activation_squared_loss
import tensorflow as tf


class Task(tf.keras.utils.Sequence):
    def __init__(self, controller, initial_joint_state=None):
        self.controller = controller
        self.plant = controller.plant
        self.last_batch_size = None
        self.last_n_timesteps = None
        self.training_iterations = 1000
        self.training_batch_size = 32
        self.training_n_timesteps = 100
        self.do_recompute_targets = False
        self.losses = {}
        self.loss_weights = {}

        if initial_joint_state is not None:
            initial_joint_state = np.array(initial_joint_state)
            if len(initial_joint_state.shape) == 1:
                initial_joint_state = initial_joint_state.reshape(1, -1)
            self.n_initial_joint_states = initial_joint_state.shape[0]
        else:
            self.n_initial_joint_states = None
        self.initial_joint_state = initial_joint_state

    @abstractmethod
    def generate(self, batch_size, n_timesteps, **kwargs):
        return

    def get_initial_state(self, batch_size):
        if self.initial_joint_state is None:
            inputs = None
        else:
            i = np.random.randint(0, self.n_initial_joint_states, batch_size)
            inputs = self.initial_joint_state[i, :]
        return self.controller.get_initial_state(batch_size=batch_size, inputs=inputs)

    def get_input_dim(self):
        [inputs, _, _] = self.generate(batch_size=1, n_timesteps=5000)
        shape = inputs.get_shape().as_list()
        return shape[-1]

    def get_losses(self):
        return [self.losses, self.loss_weights]

    def set_training_params(self, batch_size, n_timesteps, iterations):
        self.training_batch_size = batch_size
        self.training_n_timesteps = n_timesteps
        self.training_iterations = iterations

    def __getitem__(self, idx):
        [inputs, targets, init_states] = self.generate(batch_size=self.training_batch_size,
                                                       n_timesteps=self.training_n_timesteps)
        return [inputs, init_states], targets

    def __len__(self):
        return self.training_iterations


class TaskStaticTarget(Task):
    def __init__(self, controller, **kwargs):
        super().__init__(controller, **kwargs)
        self.losses = {'cartesian position': position_loss(), 'muscle state': activation_squared_loss()}
        self.loss_weights = {'cartesian position': 1, 'muscle state': 0.2}  # 0.2

    def generate(self, batch_size, n_timesteps, **kwargs):
        self.last_batch_size = batch_size
        self.last_n_timesteps = n_timesteps
        init_states = self.get_initial_state(batch_size=batch_size)
        goal_states = self.plant.draw_random_uniform_states(batch_size=batch_size)
        targets = self.plant.state2target(state=self.plant.joint2cartesian(goal_states), n_timesteps=n_timesteps)
        return [targets[:, :, 0:self.plant.space_dim], targets, init_states]


class TaskStaticTargetWithPerturbations(Task):
    def __init__(self, controller, **kwargs):
        super().__init__(controller, **kwargs)
        self.losses = {'cartesian position': position_loss(), 'muscle state': activation_squared_loss()}
        self.loss_weights = {'cartesian position': 1, 'muscle state': 0.2}

    def generate(self, batch_size, n_timesteps, **kwargs):
        self.last_batch_size = batch_size
        self.last_n_timesteps = n_timesteps
        init_states = self.get_initial_state(batch_size=batch_size)
        goal_states = self.plant.draw_random_uniform_states(batch_size=batch_size)
        targets = self.plant.state2target(state=self.plant.joint2cartesian(goal_states), n_timesteps=n_timesteps)
        perturbations = tf.constant(5., shape=(batch_size, n_timesteps, 2))
        self.controller.perturbation_dim_start = 2
        inputs = tf.concat([targets[:, :, 0:self.plant.space_dim], perturbations], axis=2)
        return [inputs, targets, init_states]


class TaskDelayedReach(Task):
    def __init__(self, controller, **kwargs):
        super().__init__(controller, **kwargs)

        self.losses = {'cartesian position': position_loss(), 'muscle state': activation_squared_loss()}
        self.loss_weights = {'cartesian position': 1, 'muscle state': 0.2}

        self.bump_length = int(kwargs.get('bump_length', 50) / 1000 / self.plant.dt)
        self.bump_height = kwargs.get('bump_height', 1)
        self.delay_range = np.array(kwargs.get('delay_range', [100, 900])) / 1000 / self.plant.dt

        self.do_recompute_targets = True

    def generate(self, batch_size, n_timesteps, **kwargs):
        self.last_batch_size = batch_size
        self.last_n_timesteps = n_timesteps
        testing_mode = kwargs.get('testing_mode', False)  # I'll get back to this soon
        init_states = self.get_initial_state(batch_size=batch_size)
        center = self.plant.joint2cartesian(init_states[0][0, :])
        goal_states = self.plant.draw_random_uniform_states(batch_size=batch_size)
        targets = self.plant.state2target(state=self.plant.joint2cartesian(goal_states),
                                          n_timesteps=n_timesteps).numpy()

        temp_inputs = []
        for i in range(batch_size):
            delay_time = generate_delay_time(self.delay_range[0], self.delay_range[1], 'random')
            bump = np.concatenate([np.zeros(delay_time), np.ones(self.bump_length)*self.bump_height,
                                   np.zeros(int(n_timesteps - delay_time - self.bump_length))])
            temp_inputs.append(np.concatenate([np.squeeze(targets[i, :, 0:2]), np.expand_dims(bump, axis=1)], axis=1))
            targets[i, 0:delay_time, :] = center

        inputs = tf.stack(temp_inputs, axis=0)
        return [inputs, tf.convert_to_tensor(targets), init_states]

    # this is not a static method because in theory you could use the generate method to save information
    # that you use here to decide how to modify the targets
    def recompute_targets(self, inputs, targets, outputs):
        # grab endpoint position and velocity
        cartesian_pos = outputs['cartesian position']
        # calculate the distance to the targets as run by the forward pass
        dist = tf.tile(tf.expand_dims(tf.sqrt(tf.reduce_sum((cartesian_pos[:, :, 0:2] - targets[:, :, 0:2])**2,
                                                            axis=2)), axis=2), tf.constant([1, 1, 2], tf.int32))
        cartesian_pos_no_vel = tf.concat([cartesian_pos[:, :, 0:2], tf.zeros_like(dist)], axis=2)
        dist = tf.concat([dist, tf.zeros_like(dist)], axis=2)
        # if the distance is less than a certain amount, replace the target with the result of the forward pass
        targets = tf.where(tf.less_equal(dist, 0.1), cartesian_pos_no_vel, targets)
        return targets


class TaskDelayedMultiReach(Task):
    def __init__(self, controller, initial_joint_state=None, **kwargs):
        super().__init__(controller, initial_joint_state=initial_joint_state)

        self.losses = {'cartesian position': position_loss(), 'muscle state': activation_squared_loss()}
        self.loss_weights = {'cartesian position': 1, 'muscle state': 0.2}

        self.bump_length = int(kwargs.get('bump_length', 50) / 1000 / self.plant.dt)
        self.bump_height = kwargs.get('bump_height', 3)
        self.delay_range = np.array(kwargs.get('delay_range', [100, 900])) / 1000 / self.plant.dt
        self.num_target = kwargs.get('num_target', 1)
            
    def generate(self, batch_size, n_timesteps, **kwargs):
        self.last_batch_size = batch_size
        self.last_n_timesteps = n_timesteps
        testing_mode = kwargs.get('testing_mode', False)  # I'll get back to this soon
        init_states = self.get_initial_state(batch_size=batch_size)
        center = self.plant.joint2cartesian(init_states[0][0, :])
        
        num_target = self.num_target
        target_list = np.zeros((batch_size, n_timesteps, 4, num_target))
        
        for tg in range(num_target):
            goal_states = self.plant.draw_random_uniform_states(batch_size=batch_size)
            target_list[:, :, :, tg] = self.plant.state2target(
                state=self.plant.joint2cartesian(goal_states),
                n_timesteps=n_timesteps).numpy()
            
        temp_inputs = []
        temp_targets = []

        sequence_time = int((num_target + 1) * target_list.shape[1] + self.delay_range[1] + self.bump_length)
        targets = np.zeros((batch_size, sequence_time, 4))

        for i in range(batch_size):
            # Create Inputs
            delay_time = generate_delay_time(self.delay_range[0], self.delay_range[1], 'random')
            bump = np.concatenate([np.zeros(n_timesteps),
                                   np.zeros(delay_time),
                                   np.ones(self.bump_length) * self.bump_height,
                                   np.zeros(int(num_target * n_timesteps) + int(self.delay_range[1] - delay_time))])

            input_tensor = np.concatenate([np.squeeze(target_list[i, :, 0:2, tr]) for tr in range(num_target)], axis=1)  # Concatenate input positions for the targets
            input_tensor = tf.repeat(input_tensor, num_target, axis=0)  # Repeat the input (num_target) times
            # Concatenate zeros for delay period
            input_tensor = np.concatenate([
                np.zeros((n_timesteps, 2 * num_target)),                                  # For Go to Center
                np.zeros((int(self.delay_range[1] + self.bump_length), 2 * num_target)),  # For remain in center and see targets
                input_tensor,                                                             # For each to targets
                ], axis=0)
            input_tensor[:n_timesteps] = np.repeat(center[0, :2], num_target)  # center      # Assign Center point for inital reach of the trial (before delay)
            # Assign first target to input for delay period
            input_tensor[n_timesteps:n_timesteps + self.delay_range[1] + self.bump_length] =\
                input_tensor[n_timesteps + self.delay_range[1] + self.bump_length+1, :]
            # append the current trial to temp list, later stacks to tensor with first dimension = batch size
            temp_inputs.append(np.concatenate([input_tensor, np.expand_dims(bump, axis=1)], axis=1))

            # Create targets (outputs)
            # ---------------
            targets[i, :n_timesteps, :] = center  # Reach to Center during delay
            targets[i, n_timesteps:n_timesteps + delay_time, :] = center  # Reach to Center during delay
            # Show True targets after go random bump
            targets[i, n_timesteps + delay_time:n_timesteps + delay_time + int(num_target * n_timesteps), :] =\
                np.concatenate([np.squeeze(target_list[i, :, :, tr]) for tr in range(num_target)], axis=0)
            # Fill the remaining time point at the end with last target (That happens due to random length of bump)
            targets[i, delay_time+(num_target*n_timesteps):, :] = target_list[i, 0, :, -1]

        inputs = tf.stack(temp_inputs, axis=0)
        return [inputs, tf.convert_to_tensor(targets), init_states]


class TaskLoadProbability(Task):
    def __init__(self, controller, **kwargs):
        super().__init__(controller, **kwargs)

        self.losses = {'cartesian position': position_loss(), 'muscle state': activation_squared_loss()}
        self.loss_weights = {'cartesian position': 1, 'muscle state': 20}

        self.fixation_time = int(np.array(kwargs.get('fixation_time', 100)) / 1000 / self.plant.dt)
        self.delay_range = np.array(kwargs.get('delay_range', [300, 900])) / 1000 / self.plant.dt

        self.do_recompute_targets = True
        self.controller.perturbation_dim_start = 2

    def generate(self, batch_size, n_timesteps, **kwargs):
        self.last_batch_size = batch_size
        self.last_n_timesteps = n_timesteps
        init_states = self.get_initial_state(batch_size=batch_size)
        center = self.plant.joint2cartesian(init_states[0][0, :]).numpy()
        goal_state = center + np.array([-0.028279, -0.042601, 0, 0])
        targets = np.tile(goal_state, (batch_size, n_timesteps, 1))
        prob_array = np.array([0, 0.25, 0.5, 0.75, 1])
        inputs = np.zeros(shape=(batch_size, n_timesteps, 4))
        for i in range(batch_size):
            input_1 = np.zeros(shape=n_timesteps)
            input_2 = np.zeros(shape=n_timesteps)
            perturbation = np.tile([0, -1], (n_timesteps, 1))  # background load
            delay_time = generate_delay_time(self.delay_range[0], self.delay_range[1], 'random')
            pert_time = delay_time+self.fixation_time
            prob = prob_array[np.random.randint(0, 5)]
            if np.greater_equal(np.random.rand(), 0.2):
                targets[i, 0:pert_time, :] = center
                if np.greater_equal(np.random.rand(),  prob):
                    pert = 0
                else:
                    pert = -2
                # The visual delay MUST be built into these inputs, otherwise the network gets immediate visual cues
                input_1[self.fixation_time + 1:] = prob
                input_2[self.fixation_time + 1:] = 1 - prob
                perturbation[pert_time:, 1] = pert
            else:
                targets[i, :, :] = center
                input_1[self.fixation_time + 1:] = prob
                input_2[self.fixation_time + 1:] = 1 - prob

            inputs[i, :, 0] = input_1
            inputs[i, :, 1] = input_2
            inputs[i, :, 2:4] = perturbation

        return [tf.convert_to_tensor(inputs, dtype=tf.float32), tf.convert_to_tensor(targets, dtype=tf.float32),
                init_states]

    # this is not a static method because in theory you could use the generate method to save information
    # that you use here to decide how to modify the targets
    def recompute_targets(self, inputs, targets, outputs):
        # grab endpoint position and velocity
        cartesian_pos = outputs['cartesian position']
        # calculate the distance to the targets as run by the forward pass
        dist = tf.sqrt(tf.reduce_sum((cartesian_pos[:, :, 0:2] - targets[:, :, 0:2])**2, axis=2))
        dist = tf.where(tf.equal(inputs[0][:, :, 3], -1.), 1000., dist)
        dist = tf.tile(tf.expand_dims(dist, axis=2), tf.constant([1, 1, 2], tf.int32))
        cartesian_pos_no_vel = tf.concat([cartesian_pos[:, :, 0:2], tf.zeros_like(dist)], axis=2)
        dist = tf.concat([dist, tf.zeros_like(dist)], axis=2)
        # if the distance is less than a certain amount, replace the target with the result of the forward pass
        targets = tf.where(tf.less_equal(dist, 0.035), cartesian_pos_no_vel, targets)
        return targets

def generate_delay_time(delay_min, delay_max, delay_mode):
    if delay_mode == 'random':
        delay_time = np.random.uniform(delay_min, delay_max)
    elif delay_mode == 'noDelayInput':
        delay_time = 0
    else:
        raise AttributeError

    return int(delay_time)
